"""Small read router with explicit native owners and fixture NAV/RAM providers."""

from __future__ import annotations

from datetime import datetime
import hashlib
import re

from .private_scope import require_shared_view
from .source import GitView, safe_path
from . import tokenslang as ts


def text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeError as exc:
        raise ts.ReadError("unsupported", "Source is not UTF-8 text.") from exc


class Hub:
    def __init__(self, view: GitView, catalog_path: str = "eutonos.read.json", meter=None):
        require_shared_view(view)
        self.meter = meter
        self.view = view
        self.catalog_path = safe_path(catalog_path)
        raw, self.catalog_source = view.read(self.catalog_path)
        self.catalog = ts.loads(text(raw))
        ts.shape(self.catalog, {"profile", "repository_id", "scope_id", "namespace", "owners",
                               "bindings", "excluded_roots", "source_paths"}, {"extensions"})
        ts.require(self.catalog["profile"] == ts.CATALOG, "unsupported", "Unsupported TokenSlang catalog.")
        for key in ("repository_id", "scope_id", "namespace"):
            ts.string(self.catalog[key])
        ts.shape(self.catalog["owners"], {"current_task"})
        ts.reference(self.catalog["owners"]["current_task"])
        self.bindings = ts.sequence(self.catalog["bindings"])
        for binding in self.bindings:
            ts.shape(binding, {"resource_id", "path", "kind", "label", "visibility", "state"},
                     {"aliases", "sha256", "start_line", "end_line", "extensions"})
            for key in ("resource_id", "label", "visibility", "state"):
                ts.string(binding[key])
            safe_path(binding["path"])
            ts.require(isinstance(binding["kind"], str) and binding["kind"] in {"docs", "source", "nav", "ram"}, "unsupported", "Unsupported provider.")
            for alias in ts.sequence(binding.get("aliases", [])):
                ts.string(alias)
            if "sha256" in binding:
                ts.require(isinstance(binding["sha256"], str) and bool(re.fullmatch(r"[0-9a-f]{64}", binding["sha256"])),
                           "invalid", "Invalid source fingerprint.")
            ts.reference({k: binding[k] for k in ("path", "start_line", "end_line") if k in binding})
        view.excluded_roots = tuple(safe_path(p) for p in ts.sequence(self.catalog["excluded_roots"]))
        self.source_paths = {safe_path(p) for p in ts.sequence(self.catalog["source_paths"])}

    def ref(self, resource_id: str) -> dict:
        return {"scope_id": self.catalog["scope_id"], "namespace": self.catalog["namespace"],
                "resource_id": resource_id}

    @staticmethod
    def _visible(binding: dict) -> bool:
        return binding["visibility"] == "public" and binding["state"] == "active"

    def _resolve(self, target: dict) -> dict:
        ts.reference(target)
        if "path" in target:
            path = safe_path(target["path"])
            owners = [b for b in self.bindings if b["path"] == path]
            ts.require(path != self.catalog_path and all(self._visible(b) and b["kind"] in {"docs", "source"} for b in owners),
                       "denied", "Source is not eligible for this public read profile.")
            ts.require(path in self.source_paths, "denied", "Locator is not an admitted direct source.")
            # Locator-only reads never fabricate a registered semantic identity.
            binding = {**target, "kind": "source", "identity_status": "unregistered"}
        else:
            ts.require(target["scope_id"] == self.catalog["scope_id"]
                       and target["namespace"] == self.catalog["namespace"],
                       "wrong-scope", "Reference is outside the selected scope/namespace.")
            matches = [b for b in self.bindings if target["resource_id"] == b["resource_id"]
                       or target["resource_id"] in b.get("aliases", [])]
            ts.require(bool(matches), "missing", "Required reference is not registered.")
            ts.require(all(self._visible(b) for b in matches), "denied", "Reference is not eligible for public reading.")
            ts.require(len(matches) == 1, "ambiguous", "Reference has competing bindings.")
            binding = {**matches[0], "identity_status": "registered"}
        # Even locator-only access must honor every registered fingerprint for a path.
        ts.require(all(self._visible(b) for b in self.bindings if b["path"] == binding["path"]),
                   "denied", "Source has an ineligible binding.")
        data, evidence = self.view.read(binding["path"])
        for b in [b for b in self.bindings if b["path"] == binding["path"]]:
            ts.require("sha256" not in b or b["sha256"] == evidence["sha256"],
                       "stale", "Binding fingerprint differs from obtained source.")
        content = text(data)
        if binding["kind"] in {"nav", "ram"}:
            controls = ts.loads(content)
            ts.require(isinstance(controls, dict) and controls.get("visibility") == "public"
                       and controls.get("state") == "active", "denied", "Record is not eligible for public reading.")
        lines = content.splitlines(keepends=True)
        if "start_line" in binding:
            ts.require(binding["end_line"] <= len(lines), "missing", "Selected line range is absent.")
            content = "".join(lines[binding["start_line"] - 1:binding["end_line"]])
            evidence["range"] = [binding["start_line"], binding["end_line"]]
        else:
            evidence["range"] = [1, len(lines)]
        evidence["text_sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return {"binding": binding, "source": evidence, "text": content}

    def _source_item(self, target: dict) -> dict:
        found = self._resolve(target)
        binding = found["binding"]
        result = {"identity_status": binding["identity_status"], "source": found["source"]}
        if binding["identity_status"] == "registered":
            result["ref"] = self.ref(binding["resource_id"])
            result["label"] = binding["label"]
        return {**result, "kind": binding["kind"], "text": found["text"]}

    def _record(self, target: dict, kind: str) -> dict:
        found = self._resolve(target)
        ts.require(found["binding"]["kind"] == kind, "unsupported", "Target does not use this provider.")
        record = ts.loads(found["text"])
        common = {"profile", "resource_id", "revision", "label", "visibility", "state", "evidence"}
        if kind == "nav":
            ts.shape(record, common | {"sources"}, {"extensions"})
            ts.require(record["profile"] == ts.NAV, "unsupported", "Unsupported NAV fixture profile.")
            refs = ts.sequence(record["sources"])
        else:
            ts.shape(record, common | {"summary", "sources", "recorded_at", "origin", "retention"},
                     {"occurred_at", "extensions"})
            ts.require(record["profile"] == ts.RAM, "unsupported", "Unsupported RAM fixture profile.")
            for field in ("summary", "origin", "retention"):
                ts.string(record[field])
            for field in ("recorded_at", "occurred_at"):
                if field in record:
                    try:
                        moment = datetime.fromisoformat(ts.string(record[field]))
                    except ValueError as exc:
                        raise ts.ReadError("invalid", "Invalid RAM event time.") from exc
                    ts.require(moment.utcoffset() is not None, "invalid", "RAM times must include a UTC offset.")
            refs = ts.sequence(record["sources"])
        for field in ("resource_id", "revision", "label"):
            ts.string(record[field])
        ts.require(record["resource_id"] == found["binding"]["resource_id"], "conflict", "Record identity mismatch.")
        ts.require(record["visibility"] == "public" and record["state"] == "active", "denied", "Record is suppressed or unavailable.")
        ts.require(record["evidence"] == "fixture-only", "unsupported", "Only fixture NAV/RAM records are supported.")
        sources = [self._resolve(ref)["source"] for ref in refs]
        return {"ref": self.ref(record["resource_id"]), "record": record,
                "source": found["source"], "resolved_sources": sources, "fixture_data": True}

    def _read(self, target: dict) -> dict:
        found = self._resolve(target)
        if found["binding"]["kind"] in {"nav", "ram"}:
            return self._record(target, found["binding"]["kind"])
        return self._source_item(target)

    def _read_page(self, target: dict, budget: int, cursor: dict | None) -> tuple[dict, bool]:
        """Page a direct source by Unicode character offset under the encoded data budget."""
        found = self._resolve(target)
        if found["binding"]["kind"] in {"nav", "ram"}:
            ts.require(cursor is None, "unsupported", "Fixture records do not accept source cursors.")
            return self._record(target, found["binding"]["kind"]), True
        binding = found["binding"]
        data = {"identity_status": binding["identity_status"], "source": found["source"],
                "kind": binding["kind"], "text": found["text"]}
        if binding["identity_status"] == "registered":
            data.update(ref=self.ref(binding["resource_id"]), label=binding["label"])
        full_text = data["text"]
        if cursor is None and len(ts.encoded(data)) <= budget:
            return data, True
        if cursor is not None:
            ts.require(cursor["commit"] == self.view.commit and
                       cursor["text_sha256"] == found["source"]["text_sha256"] and
                       cursor["target_sha256"] == hashlib.sha256(ts.encoded(target)).hexdigest(),
                       "stale", "Source cursor does not match the selected committed text.")
        start = 0 if cursor is None else cursor["offset"]
        if cursor is None and not full_text:
            raise ts.ReadError("overflow", "max_bytes cannot fit source metadata; increase the budget.")
        ts.require(start < len(full_text), "invalid", "Source cursor is past the selected text.")

        def page(end: int) -> dict:
            return {**data, "text": full_text[start:end],
                    "page": {"start": start, "end": end, "total": len(full_text),
                             "unit": "unicode-code-points", "complete": end == len(full_text)}}

        low, high = start, len(full_text)
        while low < high:
            middle = (low + high + 1) // 2
            if len(ts.encoded(page(middle))) <= budget:
                low = middle
            else:
                high = middle - 1
        ts.require(low > start, "overflow", "max_bytes cannot fit source metadata and one text character; increase the budget.")
        return page(low), False

    def _orient(self, budget: int) -> tuple[dict, list, bool]:
        owner = self.catalog["owners"]["current_task"]
        found = self._resolve(owner)
        ts.require(found["binding"]["kind"] == "docs", "unsupported", "Work owner must use the Docs adapter.")
        work = ts.work(found["text"])
        required = [self._read(ref) for ref in ts.sequence(work["required"])]
        payload = {"work": {k: v for k, v in work.items() if k != "optional"},
                   "work_source": found["source"], "required": required, "optional": []}
        ts.require(len(ts.encoded(payload)) <= budget, "overflow", "Required context exceeds max_bytes; no complete context delivered.")
        diagnostics = []
        for hint in ts.sequence(work["optional"]):
            ts.shape(hint, {"target", "why"})
            ts.string(hint["why"])
            ts.reference(hint["target"])
            try:
                item = {"why": hint["why"], "item": self._read(hint["target"])}
                proposed = {**payload, "optional": [*payload["optional"], item]}
                ts.require(len(ts.encoded(proposed)) <= budget, "overflow", "Optional context omitted by budget.")
                payload = proposed
            except ts.ReadError as exc:
                # Do not expose a hidden record's title/path or echo unvalidated metadata.
                diagnostics.append({"code": exc.code, "message": "Optional context unavailable or omitted."})
        return payload, diagnostics, not diagnostics

    def _batch(self, items: list, budget: int) -> tuple[dict, list, bool]:
        """Read bounded independent selections once while preserving every caller ID."""
        results, diagnostics, cache = [], [], {}
        required_complete = True
        for item in items:
            key = ts.encoded(item["target"])
            if key not in cache:
                try:
                    cache[key] = ("ok", self._read(item["target"]))
                except ts.ReadError as exc:
                    # Keep failures useful without echoing a denied locator or private binding.
                    cache[key] = (exc.code, None)
            outcome, data = cache[key]
            selected = {"id": item["id"], "required": item["required"], "outcome": outcome, "data": data}
            if outcome != "ok":
                selected["message"] = "Selected source was unavailable or ineligible."
                diagnostics.append({"item_id": item["id"], "code": outcome,
                                    "message": "Selected batch item was unavailable or omitted."})
                required_complete = required_complete and not item["required"]
            proposed = {"items": [*results, selected], "requested": len(items),
                        "delivered": sum(row["outcome"] == "ok" for row in [*results, selected]),
                        "deduplicated_reads": len(results) + 1 - len(cache)}
            ts.require(len(ts.encoded(proposed)) <= budget, "overflow",
                       "Read batch exceeds max_bytes; submit fewer or narrower selections.")
            results.append(selected)
        data = {"items": results, "requested": len(items),
                "delivered": sum(row["outcome"] == "ok" for row in results),
                "deduplicated_reads": len(items) - len(cache),
                "required_complete": required_complete}
        return data, diagnostics, all(row["outcome"] == "ok" for row in results)

    def handle(self, value: dict) -> dict:
        if self.meter is None:
            return self._handle(value)
        from .measurement import observe
        return observe(self.meter, lambda: self._handle(value))

    def _handle(self, value: dict) -> dict:
        self.view.reset_limits()
        if isinstance(value, dict) and value.get("profile") == "eutonos.tokenslang.communication-read.v1":
            from .effects import LocalRoot, digest
            from .communication import Board
            try:
                local = LocalRoot(self.view.root, self.view.excluded_roots)
                ts.require(digest(local.read(self.catalog_path)) == self.catalog_source["sha256"],
                           "stale", "Reopen the Hub with the current catalog before reading messages.")
                board = Board(local, denied_paths=[b["path"] for b in self.bindings if not self._visible(b)])
                ts.require(all(board.cfg[k] == self.catalog[k] for k in ("repository_id", "scope_id", "namespace")),
                           "wrong-scope", "Communication and Hub identities differ.")
                result = board.read(value)
                ts.require(digest(local.read(self.catalog_path)) == self.catalog_source["sha256"],
                           "stale", "Catalog controls changed during message retrieval.")
                return result
            except ts.ReadError as exc:
                return {"profile": value["profile"], "outcome": "error", "code": exc.code, "message": exc.message}
        if isinstance(value, dict) and value.get("profile") == "eutonos.tokenslang.ram-read.v1":
            from .effects import LocalRoot, digest
            from .ram import Store
            try:
                local = LocalRoot(self.view.root, self.view.excluded_roots)
                ts.require(digest(local.read(self.catalog_path)) == self.catalog_source["sha256"],
                           "stale", "RAM needs the current catalog/control view; reopen the Hub after catalog changes.")
                store = Store(local, denied_paths=[b["path"] for b in self.bindings if not self._visible(b)])
                ts.require(all(store.cfg[k] == self.catalog[k] for k in ("repository_id", "scope_id", "namespace")),
                           "wrong-scope", "RAM and Hub select different identities.")
                result = store.read(value)
                ts.require(digest(local.read(self.catalog_path)) == self.catalog_source["sha256"],
                           "stale", "Catalog controls changed during RAM retrieval.")
                return result
            except ts.ReadError as exc:
                return {"profile": value["profile"], "outcome": "error", "code": exc.code, "message": exc.message}
        if isinstance(value, dict) and value.get("profile") == "eutonos.tokenslang.nav-read.v1":
            from .nav import NavIndex
            try:
                index = NavIndex(self.view, denied_paths=[b["path"] for b in self.bindings if not self._visible(b)])
                ts.require(all(index.cfg[k] == self.catalog[k] for k in ("repository_id", "scope_id", "namespace")),
                           "wrong-scope", "NAV config and Hub catalog select different identities.")
                return index.handle(value)
            except ts.ReadError as exc:
                return {"profile": value["profile"], "outcome": "error", "code": exc.code, "message": exc.message}
        result = {"profile": ts.PROFILE, "language": ts.LANGUAGE,
                  "source_view": {"kind": "git-commit", "repository_id": self.catalog["repository_id"],
                                  "requested_revision": self.view.requested_revision, "commit": self.view.commit,
                                  "local_overlay": "not-observed"},
                  "catalog_source": self.catalog_source,
                  "freshness": "at-selected-commit-not-latest-claim", "verification": "retrieval-only",
                  "authorization": "no-execution-grant; current-control-view-not-established",
                  "preservation": "display-projection", "coverage": {"required_complete": False},
                  "diagnostics": [], "data": None}
        try:
            ts.request(value)
            result.update(request_id=value["request_id"], operation=value["operation"])
            ts.require("expected_commit" not in value or value["expected_commit"] == self.view.commit,
                       "conflict", "Requested commit precondition differs from obtained view.")
            operation, budget = value["operation"], value["max_bytes"]
            complete = True
            if operation == "describe":
                data = {"operations": ts.OPERATIONS, "source_adapter": "git-commit",
                        "profile": ts.PROFILE, "scope_id": self.catalog["scope_id"],
                        "namespace": self.catalog["namespace"], "effect": "read-only",
                        "unavailable": ["live-view", "delta", "writes", "nav.build", "ram.persist", "mcp", "semantic-helper"],
                        "limits": {"payload_bytes": None, "payload_budget": "caller-max_bytes", "source_bytes": None,
                                   "request_bytes": ts.MAX_SOURCE_BYTES, "result_bytes": None,
                                   "items_per_list": ts.MAX_ITEMS, "read_batch_items": 16},
                        "nav_ram": "fixture-readers-only", "visibility": "public-active-records-only"}
            elif operation == "orient":
                data, result["diagnostics"], complete = self._orient(budget)
            elif operation == "read.batch":
                data, result["diagnostics"], complete = self._batch(value["items"], budget)
            elif operation == "resolve":
                data = self._source_item(value["target"])
                del data["text"]
            elif operation in {"nav.read", "ram.read"}:
                data = self._record(value["target"], operation.split(".")[0])
            elif operation == "read":
                data, complete = self._read_page(value["target"], budget, value.get("cursor"))
            else:
                data = self._read(value["target"])
            ts.require(len(ts.encoded(data)) <= budget, "overflow", "Payload exceeds max_bytes; follow the source route with a larger or narrower request.")
            required_complete = (data.get("required_complete", True) if operation == "read.batch" else
                                 ("page" not in data if operation == "read" else True))
            result.update(outcome="ok" if complete else "partial", data=data,
                          coverage={"required_complete": required_complete, "optional_complete": complete})
        except ts.ReadError as exc:
            result.update(outcome=exc.code, diagnostics=[{"code": exc.code, "message": exc.message}], data=None)
        result["continuation"] = {"commit": self.view.commit, "catalog": self.catalog_path,
                                  "action": "inspect-exact-source-or-correct-request"}
        if isinstance(value, dict) and value.get("operation") == "orient":
            result["continuation"]["target"] = self.catalog["owners"]["current_task"]
        elif isinstance(value, dict) and "target" in value and result.get("request_id"):
            result["continuation"]["target"] = value["target"]
        if result.get("operation") == "read" and result.get("data") and "page" in result["data"]:
            page = result["data"]["page"]
            if not page["complete"]:
                result["continuation"].update(action="continue-read",
                    next_request={"profile": ts.PROFILE, "request_id": value["request_id"],
                                  "operation": "read", "purpose": value["purpose"],
                                  "max_bytes": budget, "target": value["target"],
                                  "expected_commit": self.view.commit,
                                  "cursor": {"commit": self.view.commit,
                                             "target_sha256": hashlib.sha256(ts.encoded(value["target"])).hexdigest(),
                                             "text_sha256": result["data"]["source"]["text_sha256"],
                                             "offset": page["end"]}})
        return result
