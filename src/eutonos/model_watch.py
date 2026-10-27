"""Portable native model watch. Local bounded metadata only; no model/network calls.

Lazy six-hour scans retain exact pins, confirm absence in two separated healthy
observations, and keep coalesced notices until explicit acknowledgement. Unknown
sources preserve capabilities. Pickup and catalog reads never consume notices.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import tempfile
import time

from . import model_catalog
from .local_lock import local_lock

PROFILE = "eutonos.model-watch.v2"
MAX_STATE_BYTES = 262_144
MAX_PENDING = 128
SCAN_INTERVAL = 21_600
ABSENCE_INTERVAL = 21_600
PICKUP_ITEMS = 16
LOCK_DEFERRED_HINT_AFTER = 60
MAX_DEFERRED_BYTES = 256
DOC_REFERENCES = [
    {"name": "OpenAI models", "url": "https://developers.openai.com/api/docs/models", "as_of": "2026-09-29"},
    {"name": "Anthropic models", "url": "https://platform.claude.com/docs/en/about-claude/models/overview", "as_of": "2026-09-29"},
]


class ModelWatchError(ValueError):
    """A visible failure; existing state and pending notices remain intact."""


def _error(exc):
    if isinstance(exc, ModelWatchError):
        return str(exc)[:240]
    if isinstance(exc, OSError):
        return "watch storage unavailable; existing state preserved"
    return "watch metadata unavailable; existing state preserved"


def state_path(root=None) -> Path:
    return model_catalog.watch_home() / "baseline.json"


def _at(now=None):
    value = time.time() if now is None else now
    if not _valid_time(value):
        raise ModelWatchError("watch time must be a finite nonnegative UTC epoch")
    return value


def _valid_time(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _optional_time(value):
    return value is None or _valid_time(value)


def _caps(entry):
    return {key: entry[key] for key in ("supported_efforts", "family", "effort_basis")}


def _valid_caps(entry, harness):
    if not isinstance(entry, dict):
        return False
    efforts = entry.get("supported_efforts")
    family = entry.get("family")
    basis = entry.get("effort_basis")
    return (isinstance(efforts, list) and 0 < len(efforts) <= len(model_catalog.EFFORTS)
            and all(isinstance(e, str) and e in model_catalog.EFFORTS for e in efforts)
            and len(set(efforts)) == len(efforts)
            and (family is None or isinstance(family, str) and model_catalog.FAMILIES.get(family) == harness)
            and isinstance(basis, str) and len(basis) <= 128)


def _fresh_state():
    state = {"profile": PROFILE, "sequence": 0, "pending": {}, "sources": {}}
    for harness in ("codex", "claude"):
        state["sources"][harness] = {"initialized": False, "status": "unknown", "basis": "not-scanned",
            "last_scan_at": None, "last_good_at": None, "models": {}}
    # Retained dated pins are not native observations. A healthy report must
    # confirm their absence twice before the catalog can retire these defaults.
    for model, efforts in model_catalog._CLAUDE.items():
        state["sources"]["claude"]["models"][model] = {
            "supported_efforts": list(efforts), "family": model_catalog._family(model),
            "effort_basis": f"dated-effort-assumption-{model_catalog.FALLBACK_DATE}",
            "available": True, "last_seen": None, "missing_since": None, "missing_confirmations": 0}
    return state


def _validate(state):
    if (not isinstance(state, dict) or state.get("profile") != PROFILE
            or set(state) != {"profile", "sequence", "pending", "sources"}):
        raise ValueError("profile")
    sources = state["sources"]
    if not isinstance(sources, dict) or set(sources) != {"codex", "claude"}:
        raise ValueError("sources")
    for harness, source in sources.items():
        if (not isinstance(source, dict)
                or set(source) != {"initialized", "status", "basis", "last_scan_at", "last_good_at", "models"}
                or not isinstance(source.get("initialized"), bool)
                or source.get("status") not in ("ok", "unknown", "unavailable")
                or not isinstance(source.get("basis"), str) or len(source["basis"]) > 256
                or not _optional_time(source.get("last_scan_at")) or not _optional_time(source.get("last_good_at"))):
            raise ValueError("source metadata")
        models = source["models"]
        if not isinstance(models, dict) or len(models) > 512:
            raise ValueError("model count")
        for model, entry in models.items():
            if (not model_catalog.is_exact_model_id(model)
                    or not model.startswith("claude-" if harness == "claude" else "gpt-")
                    or not _valid_caps(entry, harness)
                    or set(entry) != {"supported_efforts", "family", "effort_basis", "available",
                                      "last_seen", "missing_since", "missing_confirmations"}
                    or not isinstance(entry.get("available"), bool)
                    or not _optional_time(entry.get("last_seen")) or not _optional_time(entry.get("missing_since"))
                    or type(entry.get("missing_confirmations")) is not int
                    or entry["missing_confirmations"] not in (0, 1, 2)):
                raise ValueError("model entry")
            if ((entry["missing_since"] is None) != (entry["missing_confirmations"] == 0)
                    or not entry["available"] and entry["missing_confirmations"] != 2):
                raise ValueError("absence state")
    if type(state.get("sequence")) is not int or not 0 <= state["sequence"] <= 10**15:
        raise ValueError("sequence")
    pending_items = state["pending"]
    if not isinstance(pending_items, dict) or len(pending_items) > MAX_PENDING:
        raise ValueError("pending count")
    ids = set()
    for token, event in pending_items.items():
        if (not isinstance(event, dict) or set(event) != {"id", "source", "kind", "key",
                                                        "before", "after", "first_seen", "updated_at"}):
            raise ValueError("pending item")
        harness, kind, key = event.get("source"), event.get("kind"), event.get("key")
        event_id = event.get("id")
        if (harness not in sources or kind not in ("added", "removed", "recovered", "effort", "family_target",
                                                 "source_failure", "source_recovery")
                or not isinstance(key, str) or len(key) > 128
                or token != f"{harness}:{kind}:{key}" or not isinstance(event_id, str)
                or not event_id.startswith("model-watch-") or len(event_id) > 64 or event_id in ids
                or not _valid_time(event.get("first_seen")) or not _valid_time(event.get("updated_at"))):
            raise ValueError("pending identity")
        if kind in ("source_failure", "source_recovery"):
            if key != harness:
                raise ValueError("pending source")
        elif kind == "family_target":
            if model_catalog.FAMILIES.get(key) != harness:
                raise ValueError("pending family")
        elif not model_catalog.is_exact_model_id(key):
            raise ValueError("pending model")
        for field in ("before", "after"):
            value = event.get(field)
            valid = (value in ("ok", "unknown", "unavailable") if kind.startswith("source_")
                     else _valid_caps(value, harness) if isinstance(value, dict)
                     else model_catalog.is_exact_model_id(value))
            if value is not None and not valid:
                raise ValueError("pending value")
        ids.add(event_id)
    return state


def _read_state(path):
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_STATE_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ModelWatchError("watch baseline is unreadable") from exc
    try:
        if len(raw) > MAX_STATE_BYTES:
            raise ValueError("oversized")
        return _validate(json.loads(raw))
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise ModelWatchError("watch baseline is corrupt, incompatible or oversized; preserved for recovery") from exc


def _deferred_path(path):
    return path.with_name("scan-deferred.json")


def _clear_deferred(path):
    try:
        _deferred_path(path).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass  # Advisory metadata must never break a scan.


def _deferred_hint(path, at, *, record=False):
    """Read a small first-deferral marker; never wait for the scan lock."""
    marker = _deferred_path(path)
    try:
        if record:
            marker.parent.mkdir(parents=True, exist_ok=True)
            try:
                with marker.open("x", encoding="utf-8") as handle:
                    json.dump({"first_deferred_at": at}, handle)
            except FileExistsError:
                pass
        with marker.open("rb") as handle:
            raw = handle.read(MAX_DEFERRED_BYTES + 1)
        if len(raw) > MAX_DEFERRED_BYTES:
            return None
        first = json.loads(raw).get("first_deferred_at")
        if _valid_time(first) and at >= first + LOCK_DEFERRED_HINT_AFTER:
            return f"Model watch lazy scans deferred by a held lock for at least {LOCK_DEFERRED_HINT_AFTER} seconds since the last completed scan; holder unknown"
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def _write_state(path, state):
    try:
        _validate(state)
    except (ValueError, TypeError, KeyError) as exc:
        raise ModelWatchError("watch state capacity or shape would be exceeded; existing state preserved") from exc
    raw = (json.dumps(state, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(raw) > MAX_STATE_BYTES:
        raise ModelWatchError("watch baseline exceeds size cap; existing state preserved")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".baseline-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _observations(root, *, codex_cache_path=None, claude_models_path=None, harnesses=("codex", "claude")):
    readers = {"codex": lambda: model_catalog.read_codex_capabilities(codex_cache_path),
               "claude": lambda: model_catalog.read_claude_capabilities(root, path=claude_models_path)}
    sources = {harness: readers[harness]() for harness in harnesses}
    for source in sources.values():
        if source.get("invalid_items"):
            source["status"], source["basis"] = "unavailable", "partly-malformed-source"
    return sources


def _targets(models, harness):
    targets = {}
    for family, owner in model_catalog.FAMILIES.items():
        if owner != harness:
            continue
        matches = [model for model, entry in models.items() if entry["available"]
                   and entry["family"] == family and "preview" not in model and "experimental" not in model]
        if matches:
            targets[family] = max(matches, key=lambda model: (model_catalog._version(model), model))
    return targets


def _event(state, harness, kind, key, before, after, at):
    token = f"{harness}:{kind}:{key}"
    item = state["pending"].get(token)
    if item is None:
        if len(state["pending"]) >= MAX_PENDING:
            raise ModelWatchError("pending model notices reached capacity; acknowledge notices before scanning")
        state["sequence"] += 1
        item = {"id": f"model-watch-{state['sequence']}", "source": harness, "kind": kind,
                "key": key, "before": before, "after": after, "first_seen": at, "updated_at": at}
    else:
        # Fence a stale browser acknowledgement from consuming a newer
        # coalesced observation with the same semantic key.
        state["sequence"] += 1
        item = {**item, "id": f"model-watch-{state['sequence']}", "after": after, "updated_at": at}
    state["pending"][token] = item


def _comparison(state, observations, at):
    saved = state or _fresh_state()
    updated = deepcopy(saved)
    result = {"changed": False, "initialized": state is None, "new_families": [],
              "sources": {}, "doc_references": DOC_REFERENCES}
    for harness, observed in observations.items():
        prior, source = saved["sources"][harness], updated["sources"][harness]
        good, initialized = observed["status"] == "ok", prior["initialized"]
        source.update(status=observed["status"], basis=observed["basis"], last_scan_at=at)
        old, new = prior["models"], source["models"]
        source_changed = False
        if initialized and prior["last_scan_at"] is not None and prior["status"] != observed["status"]:
            if good:
                _event(updated, harness, "source_recovery", harness, prior["status"], "ok", at)
                source_changed = True
            elif prior["status"] == "ok" or f"{harness}:source_failure:{harness}" in updated["pending"]:
                _event(updated, harness, "source_failure", harness, prior["status"], observed["status"], at)
                source_changed = True
        added, removed, recovered, efforts = [], [], [], []
        if good:
            source["initialized"], source["last_good_at"] = True, at
            for model, entry in observed["models"].items():
                caps = _caps(entry)
                previous = old.get(model)
                new[model] = {**caps, "available": True, "last_seen": at,
                              "missing_since": None, "missing_confirmations": 0}
                if initialized:
                    if previous is None:
                        added.append({"id": model, "classification": "known_family" if caps["family"] else "new_family", **caps})
                        _event(updated, harness, "added", model, None, caps, at)
                    else:
                        if not previous["available"]:
                            recovered.append(model)
                            _event(updated, harness, "recovered", model, None, caps, at)
                        if _caps(previous) != caps:
                            efforts.append({"id": model, "before": _caps(previous), "after": caps})
                            _event(updated, harness, "effort", model, _caps(previous), caps, at)
            for model in set(old) - set(observed["models"]):
                entry = new[model]
                if not entry["available"]:
                    continue
                if entry["missing_since"] is None:
                    entry["missing_since"], entry["missing_confirmations"] = at, 1
                elif at - entry["missing_since"] >= ABSENCE_INTERVAL:
                    entry["available"], entry["missing_confirmations"] = False, 2
                    if initialized:
                        removed.append(model)
                        _event(updated, harness, "removed", model, _caps(entry), None, at)
        before, after = _targets(old, harness), _targets(new, harness)
        targets = [{"family": family, "before": before.get(family), "after": after.get(family)}
                   for family in sorted(set(before) | set(after)) if before.get(family) != after.get(family)] if initialized else []
        for target in targets:
            _event(updated, harness, "family_target", target["family"], target["before"], target["after"], at)
        delta = source_changed or bool(added or removed or recovered or efforts or targets)
        result["changed"] |= delta
        result["sources"][harness] = {"status": observed["status"], "basis": observed["basis"],
            "baseline_initialized": source["initialized"], "baseline_established": good and not initialized,
            "baseline_preserved": not good, "model_count": len(new), "added": sorted(added, key=lambda e: e["id"]),
            "removed": sorted(removed), "recovered": sorted(recovered), "effort_changes": efforts,
            "family_target_changes": targets}
        result["new_families"].extend({"source": harness, "id": entry["id"]} for entry in added
                                      if entry["classification"] == "new_family")
    result["pending_count"] = len(updated["pending"])
    return result, updated


def _scan(root, at, *, due_interval=None, lock_timeout=2, codex_cache_path=None, claude_models_path=None):
    entered = False
    lock_attempted = False
    try:
        path = state_path(root)
        lock_attempted = True
        with local_lock(path.parent, timeout=lock_timeout):
            entered = True
            _clear_deferred(path)
            prior = _read_state(path)
            harnesses = ("codex", "claude")
            if due_interval is not None and prior is not None:
                harnesses = tuple(h for h, s in prior["sources"].items()
                                  if s["last_scan_at"] is None or at < s["last_scan_at"]
                                  or at - s["last_scan_at"] >= due_interval)
                if not harnesses:
                    return {"status": "not-due", "scanned": False, "changed": False, "written": False,
                            "pending_count": len(prior["pending"]), "path": str(path),
                            "next_due_at": min(s["last_scan_at"] + due_interval for s in prior["sources"].values())}
            observations = _observations(root, codex_cache_path=codex_cache_path,
                                          claude_models_path=claude_models_path, harnesses=harnesses)
            result, updated = _comparison(prior, observations, at)
            written = prior != updated or not path.exists()
            if written:
                _write_state(path, updated)
    except TimeoutError as exc:
        if due_interval is not None and lock_attempted and not entered:
            # Another scanner owns the transaction. This lazy caller can wait
            # for its next trigger; never read/scan unlocked or report damage.
            hint = _deferred_hint(path, time.time(), record=True)
            return {"status": "deferred", "reason": "scan-in-progress", "scanned": False,
                    "changed": False, "written": False, "pending_count": None, "next_due_at": None,
                    **({"hint": hint} if hint else {})}
        if lock_attempted and not entered:
            raise ModelWatchError("watch metadata lock is busy; baseline unchanged") from exc
        raise ModelWatchError("watch scan timed out; existing state preserved") from exc
    except OSError as exc:
        raise ModelWatchError("watch storage unavailable; existing state preserved") from exc
    return {**result, "status": "ok", "scanned": True, "written": written, "path": str(path),
            "next_due_at": min(s["last_scan_at"] + (due_interval or SCAN_INTERVAL)
                               for s in updated["sources"].values() if s["last_scan_at"] is not None)}


def scan(root=None, *, now=None, codex_cache_path=None, claude_models_path=None):
    return _scan(root, _at(now), codex_cache_path=codex_cache_path, claude_models_path=claude_models_path)


def scan_if_due(root=None, *, now=None, interval=SCAN_INTERVAL):
    try:
        if not _valid_time(interval) or interval <= 0:
            raise ModelWatchError("watch interval must be positive")
        result = _scan(root, _at(now), due_interval=interval, lock_timeout=0.05)
    except Exception as exc:
        result = {"status": "attention-required", "scanned": False, "changed": False,
                  "written": False, "pending_count": 0, "next_due_at": None, "error": _error(exc)}
    # An explicit clock is used by deterministic native scans and fixtures.
    # Real pickup/dashboard/runner calls have no clock and can schedule the
    # independent official-doc pass without waiting for its network reads.
    if now is None:
        try:
            from . import harness_docs_watch
            result["documentation"] = harness_docs_watch.scan_if_due(root=root)
        except Exception:
            result["documentation"] = {"status": "attention-required",
                                       "error": "documentation watch unavailable"}
    return result


def diff(root=None, *, now=None, codex_cache_path=None, claude_models_path=None):
    path = state_path(root)
    observations = _observations(root, codex_cache_path=codex_cache_path, claude_models_path=claude_models_path)
    result, _ = _comparison(_read_state(path), observations, _at(now))
    return {**result, "path": str(path)}


def baseline(root=None):
    path = state_path(root)
    state = _read_state(path)
    return {"found": state is not None, "path": str(path), "state": state, "doc_references": DOC_REFERENCES}


def _internals_items():
    """Local, no-network watch items from Crosstalk's contract check of the Hermes internals its plugin relies on
    (``crosstalk_link``). Isolated: any failure just means no such items."""
    try:
        from . import crosstalk_link
        return crosstalk_link.watch_items()
    except Exception:
        return []


def pending(root=None):
    native_error = None
    try:
        path = state_path(root)
        state = _read_state(path)
        native = list((state or {"pending": {}})["pending"].values())
    except Exception as exc:
        native, native_error = [], _error(exc)
        path = None
    try:
        from . import harness_docs_watch
        docs = harness_docs_watch.pending(root)
    except Exception:
        docs = {"status": "attention-required", "items": [], "error": "documentation watch unavailable"}
    items = sorted(native + docs["items"] + _internals_items(), key=lambda item: (item["first_seen"], item["id"]))
    error = native_error or docs.get("error")
    return {"status": "attention-required" if error else "ok", "count": len(items), "items": items,
            **({"path": str(path)} if path else {}), **({"error": error} if error else {})}


def ack(root=None, ids=None):
    if (not isinstance(ids, list) or len(ids) > MAX_PENDING
            or any(not isinstance(i, str) or not i or len(i) > 64 for i in ids)):
        raise ModelWatchError("ack requires an explicit bounded list of notice IDs")
    from . import harness_docs_watch
    internals_ids = [item for item in ids if item.startswith("hermes-internals-")]
    ids = [item for item in ids if not item.startswith("hermes-internals-")]
    docs_ids = [item for item in ids if item.startswith("harness-docs-watch-")]
    native_ids = [item for item in ids if not item.startswith("harness-docs-watch-")]
    # Validate both stores before changing either. A later storage failure may
    # still yield a partial result, which is reported with the IDs read back.
    acknowledged = []
    try:
        path = state_path(root)
        if native_ids:
            _read_state(path)
        if docs_ids:
            docs_before = harness_docs_watch.pending(root)
            if docs_before["status"] != "ok":
                raise ModelWatchError(docs_before.get("error", "documentation watch unavailable"))
        if docs_ids:
            acknowledged.extend(harness_docs_watch.ack(root, ids=docs_ids)["acknowledged"])
        if internals_ids:
            from . import crosstalk_link
            acknowledged.extend(crosstalk_link.watch_ack(internals_ids))
        if native_ids:
            with local_lock(path.parent, timeout=2):
                state = _read_state(path)
                if state is not None:
                    wanted = set(native_ids)
                    confirmed = sorted(event["id"] for event in state["pending"].values() if event["id"] in wanted)
                    if confirmed:
                        state["pending"] = {key: event for key, event in state["pending"].items() if event["id"] not in wanted}
                        _write_state(path, state)
                        acknowledged.extend(confirmed)
    except Exception as exc:
        if not docs_ids and not internals_ids:
            if isinstance(exc, TimeoutError):
                raise ModelWatchError("watch metadata lock is busy; acknowledgement unchanged") from exc
            if isinstance(exc, OSError):
                raise ModelWatchError("watch storage unavailable; acknowledgement unchanged") from exc
            raise
        current = pending(root)
        return {"status": "attention-required", "acknowledged": sorted(acknowledged),
                "count": current["count"], "path": str(path), "error": _error(exc)}
    current = pending(root)
    return {"status": current["status"], "acknowledged": sorted(acknowledged),
            "count": current["count"], "path": str(path),
            **({"error": current["error"]} if current.get("error") else {})}


def catalog_snapshot(root=None):
    try:
        state = _read_state(state_path(root))
        if state is None:
            # Missing retained evidence is unknown, including a removed state
            # file after a healthy scan. The catalog owns last-good recovery;
            # this pure reader must not manufacture a successful empty view.
            return {"sources": {}, "pending_count": 0,
                    "error": "watch baseline is missing; retained evidence is unknown"}
        sources = {}
        for harness, source in state["sources"].items():
            sources[harness] = {key: source[key] for key in ("initialized", "status", "basis", "last_scan_at", "last_good_at")}
            sources[harness]["models"] = {model: {key: entry[key] for key in (
                "supported_efforts", "family", "effort_basis", "available", "last_seen", "missing_since")}
                for model, entry in source["models"].items()}
        return {"sources": sources, "pending_count": len(state["pending"])}
    except Exception as exc:
        return {"sources": {}, "pending_count": 0, "error": _error(exc)}


def pickup_hint(root=None):
    native_error = None
    try:
        state = _read_state(state_path(root))
        hint = _deferred_hint(state_path(root), _at())
    except Exception as exc:
        state, hint, native_error = None, None, _error(exc)
    try:
        from . import harness_docs_watch
        docs = harness_docs_watch.pickup_hint(root)
    except Exception:
        docs = {"status": "attention-required", "pending_count": 0, "items": [],
                "pending_truncated": False, "sources": {}, "error": "documentation watch unavailable"}
    internals = _internals_items()
    if state is None and docs is None and not hint and not native_error and not internals:
        return None
    items = sorted((list(state["pending"].values()) if state else [])
                   + (docs["items"] if docs else []) + internals, key=lambda item: (item["first_seen"], item["id"]))
    total = (len(state["pending"]) if state else 0) + (docs["pending_count"] if docs else 0) + len(internals)
    sources = ({h: {key: s[key] for key in ("initialized", "status", "basis", "last_scan_at", "last_good_at")}
                for h, s in state["sources"].items()} if state else {})
    error = native_error or (docs.get("error") if docs else None)
    return {"status": "attention-required" if error else "ok",
            "pending_count": total, "items": items[:PICKUP_ITEMS],
            **({"hint": hint} if hint else {}),
            "pending_truncated": total > PICKUP_ITEMS or bool(docs and docs["pending_truncated"]),
            "sources": sources,
            **({"documentation": {"sources": docs["sources"], "status": docs["status"]}} if docs else {}),
            **({"error": error} if error else {})}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("scan", "scan-if-due", "check", "diff", "baseline", "pending", "ack"):
        item = sub.add_parser(name)
        item.add_argument("--root", default=None)
        if name in ("scan", "check", "diff"):
            item.add_argument("--codex-cache", dest="codex_cache_path")
            item.add_argument("--claude-models", dest="claude_models_path")
        if name == "ack":
            item.add_argument("--id", dest="ids", action="append", required=True)
    args = vars(parser.parse_args(argv))
    command, root = args.pop("command"), args.pop("root")
    try:
        if command == "check":
            from . import harness_docs_watch
            native = scan(root, **args)
            try:
                docs = harness_docs_watch.scan(root)
            except Exception:
                docs = {"status": "attention-required", "error": "documentation watch unavailable; prior evidence preserved"}
            result = {"status": "ok" if docs["status"] == "ok" else "attention-required",
                      "native": native, "documentation": docs, "pending": pending(root)}
        else:
            result = {"scan": scan, "scan-if-due": scan_if_due, "diff": diff,
                      "baseline": baseline, "pending": pending, "ack": ack}[command](root, **args)
    except (ModelWatchError, OSError) as exc:
        print(json.dumps({"status": "attention-required", "error": _error(exc)}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 2 if result.get("status") == "attention-required" else 0


if __name__ == "__main__":
    raise SystemExit(main())
