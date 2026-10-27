"""Bounded Stop checkpoint checks, not an authenticated action/permission fence.

Native turn IDs scope Codex debt. A missing turn uses an explicitly local prompt
boundary, labeled registered-checkpoint rather than verified native turn. Claude/native-log mapping
remains a qualification boundary. Semantic assessment
belongs to the agent; stage finish cannot be inferred from tool activity.

Hot path: direct files only, capped state/proof 16 KiB, capture 64 KiB, RAM 256
KiB. No transcript parsing, host locks, directory scans, subprocesses or calls.
Only publish() (normal workflow execution, never the hook) uses native readback
and Git validation. An explicit closeout_debt_root selects hook state separately
from workflow-root evidence, pinned to linked worktrees of the same repository.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from .hidden_process import no_window
import sys
import uuid

PROFILE = "eutonos.closeout-debt.v1"
PROOF = "eutonos.closeout-debt-proof.v1"
MAX_STATE = 16384
MAX_CAPTURE = 65536
MAX_RAM = 262144
SCOPE_RE = re.compile(r"[0-9a-f]{32}\Z")
TOKEN_RE = re.compile(r"\[eutonos-closeout:([0-9a-f]{32})\]")


def _encoded(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except RecursionError as exc:
        raise ValueError("closeout metadata nesting exceeds parser limit") from exc


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _runtime_command():
    # Installed hooks run under their selected EUTONOS interpreter. Do not
    # point an acknowledgement at a possibly unrelated system Python.
    executable = ("& '" + sys.executable.replace("'", "''") + "'" if os.name == "nt"
                  else shlex.quote(sys.executable))
    return executable + " -I -B -m eutonos.closeout_debt"


def _dir(root):
    # Unlike shared settings storage, debt belongs to this exact checkout.
    return Path(root).resolve() / ".eutonos-local" / "hooks" / "closeout"


def _bytes(path, cap=MAX_STATE):
    with Path(path).open("rb") as stream:
        raw = stream.read(cap + 1)
    if len(raw) > cap:
        raise ValueError("closeout metadata exceeds byte cap")
    return raw


def _read(path, cap=MAX_STATE):
    raw = _bytes(path, cap)
    try:
        value = json.loads(raw)
    except RecursionError as exc:
        raise ValueError("closeout metadata nesting exceeds parser limit") from exc
    if not isinstance(value, dict):
        raise ValueError("closeout metadata must be an object")
    return value, raw


def _write(path, value):
    raw = _encoded(value)
    if len(raw) > MAX_STATE:
        raise ValueError("closeout metadata exceeds byte cap")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        temp.write_bytes(raw)
        os.replace(temp, path)
        if _read(path)[0] != value:
            raise ValueError("closeout metadata readback differs")
    finally:
        temp.unlink(missing_ok=True)


def _path(root, scope, suffix=".json"):
    if not isinstance(scope, str) or not SCOPE_RE.fullmatch(scope):
        raise ValueError("invalid closeout scope hash")
    return _dir(root) / (scope + suffix)


def _owner(root, payload, app):
    session = payload.get("session_id")
    agent = payload.get("agent_id")
    if app not in {"codex", "claude"} or not isinstance(session, str) or not session or len(session) > 512:
        raise ValueError("hook session identity unavailable")
    if agent is not None and (not isinstance(agent, str) or not agent or len(agent) > 512):
        raise ValueError("hook agent identity unavailable")
    return {"app": app, "checkout_root": str(Path(root).resolve()),
            "session_id": session, "agent_id": agent}


def _active_path(root, owner):
    return _dir(root) / ("active-" + _hash(_encoded(owner))[:32] + ".json")


def _uncertain(root, scope):
    return _path(root, scope, ".uncertain").exists()


def _mark_uncertain(root, scope):
    path = _path(root, scope, ".uncertain")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(b'{"state":"unresolved-contention"}')
    except FileExistsError:
        pass  # Sticky: no successful mutation clears it.


@contextmanager
def _mutation(root, name, scope=None):
    if not (SCOPE_RE.fullmatch(name) or (name.startswith("owner-") and SCOPE_RE.fullmatch(name[6:]))):
        raise ValueError("invalid mutation scope")
    path = _dir(root) / (name + ".mutation")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(b'{}')
    except FileExistsError:
        if scope:
            _mark_uncertain(root, scope)
        raise ValueError("closeout mutation contended; uncertainty retained")
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def load(root, scope):
    record = _read(_path(root, scope))[0]
    if (not isinstance(record.get("scope"), dict) or record.get("profile") != PROFILE or record.get("scope_id") != scope
            or record.get("scope", {}).get("checkout_root") != str(Path(root).resolve())):
        raise ValueError("closeout scope differs from checkout")
    if type(record.get("generation")) is not int or record["generation"] < 0:
        raise ValueError("invalid closeout observation generation")
    scope_fields = {"app", "checkout_root", "session_id", "agent_id", "turn_id", "local_boundary"}
    binding = record["scope"]
    if set(binding) != scope_fields or _owner(root, binding, binding.get("app")) != {k: binding[k] for k in ("app", "checkout_root", "session_id", "agent_id")}:
        raise ValueError("invalid closeout scope identity")
    if binding["turn_id"] is None:
        if not isinstance(binding["local_boundary"], str) or not SCOPE_RE.fullmatch(binding["local_boundary"]):
            raise ValueError("invalid local hook boundary")
    elif (not isinstance(binding["turn_id"], str) or not binding["turn_id"] or len(binding["turn_id"]) > 512
          or binding["local_boundary"] is not None):
        raise ValueError("invalid native hook boundary")
    if _hash(_encoded(record["scope"]))[:32] != scope:
        raise ValueError("closeout scope hash differs")
    return record


def observe(root, payload, app):
    owner = _owner(root, payload, app)
    name = "owner-" + _hash(_encoded(owner))[:32]
    try:
        with _mutation(root, name):
            return _observe(root, payload, app)
    except ValueError:
        # An overlapping boundary/tool event must not let old active proof win.
        try:
            active = _read(_active_path(root, owner))[0]
            _mark_uncertain(root, active["scope_id"])
        except (OSError, ValueError, KeyError):
            pass
        if isinstance(payload.get("turn_id"), str) and payload.get("hook_event_name") not in {"SubagentStart", "SubagentStop"}:
            candidate = {**owner, "turn_id": payload["turn_id"], "local_boundary": None}
            _mark_uncertain(root, _hash(_encoded(candidate))[:32])
        raise


def _observe(root, payload, app):
    """Open exact/native or explicit local debt; later tool activity reopens it."""
    owner = _owner(root, payload, app)
    active_path = _active_path(root, owner)
    try:
        active = _read(active_path)[0]
        record = load(root, active["scope_id"])
    except (OSError, ValueError, KeyError):
        active, record = {}, None
    event = payload.get("hook_event_name")
    turn = payload.get("turn_id")
    if turn is not None and (not isinstance(turn, str) or not turn or len(turn) > 512):
        raise ValueError("invalid hook turn identity")
    if event in {"SubagentStart", "SubagentStop"} and owner["agent_id"]:
        # This event can carry the parent's turn. Reuse only this exact child's
        # recorded scope; never reinterpret the parent turn as child identity.
        if event == "SubagentStop" and record is not None:
            return record
        turn = None
    # A Stop continuation is a synthetic prompt; only this owner's existing
    # create-once guard can associate its opaque token with original debt.
    token = TOKEN_RE.match(str(payload.get("prompt", ""))) if event == "UserPromptSubmit" else None
    if token:
        try:
            guard = _read(_path(root, token[1], ".guard"))[0]
            outstanding = guard == {"scope_id": token[1], "pending": True}
        except (OSError, ValueError):
            outstanding = False
        if (not outstanding or active.get("scope_id") != token[1]
                or not all(record["scope"].get(k) == v for k, v in owner.items())
                or not _path(root, token[1], ".guard").exists()):
            token = None  # Treat stale/copied text as a real new prompt boundary.
        else:
            active = {"scope_id": token[1], "event_turn": turn}
    new_prompt = event in {"UserPromptSubmit", "SubagentStart"} and not token
    matches = record is not None and active.get("event_turn") == turn
    if not matches or new_prompt:
        scope = {**owner, "turn_id": turn, "local_boundary": None if turn else uuid.uuid4().hex}
        scope_id = _hash(_encoded(scope))[:32]
        path = _path(root, scope_id)
        if path.exists():
            record = load(root, scope_id)
        else:
            transcript = payload.get("agent_transcript_path") if owner["agent_id"] else payload.get("transcript_path")
            record = {"profile": PROFILE, "scope_id": scope_id, "scope": scope,
                      "native_log": transcript if isinstance(transcript, str) and len(transcript) <= 2048 else None,
                      "generation": 0, "state": "open", "assessment": None}
        active = {"scope_id": scope_id, "event_turn": turn}
    if event in {"Stop", "SubagentStop"} and matches:
        return record  # Stop never writes an older record over newer activity.
    with _mutation(root, record["scope_id"], record["scope_id"]):
        path = _path(root, record["scope_id"])
        if path.exists():
            record = load(root, record["scope_id"])
        if event == "PreToolUse" or new_prompt:
            record["generation"] += 1
            record["state"] = "open"
            record["assessment"] = None
        _write(path, record)
        if token:
            _write(_path(root, record["scope_id"], ".guard"), {"scope_id": record["scope_id"], "pending": False})
    _write(active_path, active)
    return record


def assess(root, scope, disposition, reason):
    if disposition not in {"noop", "blocked"} or not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise ValueError("explicit noop/blocked assessment with bounded reason required")
    with _mutation(root, scope, scope):
        record = load(root, scope)
        if _uncertain(root, scope) and disposition != "blocked":
            raise ValueError("uncertain checkpoint requires truthful blocker acknowledgement")
        record["assessment"] = {"disposition": disposition, "reason": reason,
                                "generation": record["generation"]}
        record["state"] = "acknowledged-" + disposition
        _write(_path(root, scope), record)
        if _uncertain(root, scope) and disposition != "blocked":
            raise ValueError("assessment overlapped uncertain activity; blocker acknowledgement required")
    return {"outcome": record["state"], "scope_id": scope, "native_verified": False,
            "qualification": "Agent assessment only; no RAM write or stage filing is claimed."}


def _inside(root, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or "\\" in relative:
        raise ValueError("proof requires a safe relative path")
    base = Path(root).resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base):
        raise ValueError("proof path escapes checkout")
    return path


def _file_proof(root, relative, cap):
    value, raw = _read(_inside(root, relative), cap)
    return value, {"path": relative, "sha256": _hash(raw)}


def _git_binding(root):
    """Capped direct Git links, also rechecked by the hook without Git calls."""
    base = Path(root).resolve()
    marker = base / ".git"
    files = []

    def link(path):
        raw = _bytes(path)
        files.append({"path": str(path), "sha256": _hash(raw)})
        return raw.decode("utf-8").strip()

    if marker.is_dir():
        git_dir = marker.resolve()
        common = git_dir
    else:
        text = link(marker)
        if not text.startswith("gitdir: ") or "\n" in text:
            raise ValueError("invalid checkout Git link")
        git_dir = (base / text[8:]).resolve()
        common = (git_dir / link(git_dir / "commondir")).resolve()
        if Path(link(git_dir / "gitdir")).resolve() != marker:
            raise ValueError("Git worktree reverse link differs from checkout")
    if not git_dir.is_dir() or not common.is_dir():
        raise ValueError("checkout Git metadata unavailable")
    return {"root": str(base), "git_dir": str(git_dir), "common_dir": str(common), "files": files}


def _evidence_binding(debt_root, work_root):
    """Producer-only Git validation; never infer a sibling evidence worktree."""
    bindings = [_git_binding(path) for path in (debt_root, work_root)]
    for binding in bindings:
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        try:
            result = subprocess.run(["git", "-c", "safe.directory=" + binding["root"], "-C", binding["root"],
                                     "rev-parse", "--show-toplevel", "--absolute-git-dir", "--git-common-dir"],
                                    env=env, capture_output=True, text=True, timeout=5, **no_window())
        except subprocess.TimeoutExpired as exc:
            raise ValueError("checkout Git validation timed out") from exc
        paths = result.stdout.strip().splitlines()
        if result.returncode or len(paths) != 3 or any(
                str((Path(binding["root"]) / value).resolve()) != binding[key]
                for key, value in zip(("root", "git_dir", "common_dir"), paths)):
            raise ValueError("checkout Git identity differs from pinned links")
    if bindings[0]["common_dir"] != bindings[1]["common_dir"]:
        raise ValueError("closeout evidence belongs to another Git repository")
    return {"debt": bindings[0], "work": bindings[1]}


def _evidence_root(root, record, proof):
    binding = proof.get("evidence")
    if binding != record.get("evidence"):
        raise ValueError("selected closeout evidence root differs from checkpoint")
    if binding is None:
        return Path(root).resolve()  # Existing same-checkout proofs stay valid.
    if not isinstance(binding, dict) or set(binding) != {"debt", "work"}:
        raise ValueError("invalid closeout evidence root binding")
    if (binding["debt"] != _git_binding(root)
            or binding["work"] != _git_binding(binding["work"]["root"])
            or binding["debt"]["common_dir"] != binding["work"]["common_dir"]):
        raise ValueError("closeout evidence Git binding changed")
    return Path(binding["work"]["root"])


def _current_clean(root, record, state):
    if _uncertain(root, record["scope_id"]) or load(root, record["scope_id"])["generation"] != record["generation"]:
        raise ValueError("checkpoint observation changed or became uncertain")
    return state


def _consume(root, scope):
    guard = _path(root, scope, ".guard")
    if guard.exists():
        _write(guard, {"scope_id": scope, "pending": False})


def _verified(root, record):
    if _uncertain(root, record["scope_id"]):
        raise ValueError("checkpoint has sticky observation uncertainty")
    proof = _read(_path(root, record["scope_id"], ".proof.json"))[0]
    if (proof.get("profile") != PROOF or proof.get("scope") != record["scope"]
            or proof.get("generation") != record["generation"]):
        raise ValueError("proof scope/observation boundary differs")
    work_root = _evidence_root(root, record, proof)
    if proof.get("state") == "submitted" and proof.get("request_ids"):
        if not isinstance(proof["request_ids"], list) or len(proof["request_ids"]) > 16:
            raise ValueError("invalid submitted checkpoint metadata")
        return _current_clean(root, record, "submitted")
    if proof.get("state") != "verified-prefinal":
        raise ValueError("no verified pre-final checkpoint")
    if proof.get("counts_completed") is not False:
        raise ValueError("checkpoint cannot claim native final completion")
    article, article_meta = _file_proof(work_root, proof["ram"]["path"], MAX_RAM)
    if article_meta != proof["ram"] or article.get("identity") != proof["ram_identity"] or article.get("revision") != proof["ram_revision"]:
        raise ValueError("RAM bytes/revision changed after verified readback")
    if proof.get("basis") == "registered-checkpoint":
        return _current_clean(root, record, "verified-prefinal")
    capture, capture_meta = _file_proof(work_root, proof["capture"]["path"], MAX_CAPTURE)
    if (capture_meta != proof["capture"] or proof.get("basis") != "native-event"
            or capture.get("thread_id") != proof["native"]["thread_id"] or capture.get("turn_id") != proof["native"]["turn_id"]
            or capture.get("stage") != "pre-final" or capture.get("counts_completed") is not False
            or capture.get("disposition") != "captured" or capture.get("assertion", {}).get("native_proof") != "verified-readback"
            or article.get("revision") != capture["ram"]["revision"]):
        raise ValueError("capture does not pin exact current native RAM")
    return _current_clean(root, record, "verified-prefinal")


def check(root, payload, app, reminder=""):
    """One create-once continuation shared by Stop and SubagentStop; fail open."""
    try:
        if not payload.get("turn_id") and not reminder:
            owner = _owner(root, payload, app)
            if not _active_path(root, owner).exists():
                return {}, "unresolved-hook-boundary-unavailable"
        record = observe(root, payload, app)
        with _mutation(root, record["scope_id"], record["scope_id"]):
            return _check_scope(root, load(root, record["scope_id"]), payload, reminder)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}, "unresolved-guard-or-identity-unavailable"


def _check_scope(root, record, payload, reminder):
    try:
        state = _verified(root, record)
    except (OSError, ValueError, KeyError, TypeError):
        state = None
    assessment = record.get("assessment")
    acknowledged = (isinstance(assessment, dict) and assessment.get("generation") == record["generation"]
                 and assessment.get("disposition") in {"noop", "blocked"} and isinstance(assessment.get("reason"), str)
                 and bool(assessment["reason"].strip())
                 and (assessment["disposition"] == "blocked" or not _uncertain(root, record["scope_id"])))
    resolved = state or (record["state"] if acknowledged else None)
    if resolved and not reminder:
        _consume(root, record["scope_id"])
        return {}, resolved
    if payload.get("stop_hook_active"):
        _consume(root, record["scope_id"])
        return {}, resolved or "unresolved-already-continued"
    scope = record["scope_id"]
    guard = _path(root, scope, ".guard")
    try:
        with guard.open("xb") as stream:
            stream.write(_encoded({"scope_id": scope, "pending": True}))
    except FileExistsError:
        _consume(root, scope)
        return {}, "unresolved-continuation-spent"
    if _read(guard)[0] != {"scope_id": scope, "pending": True}:
        raise ValueError("continuation guard readback failed")
    prefix = "[eutonos-closeout:" + scope + "] "
    if resolved:
        return {"decision": "block", "reason": prefix + reminder}, "optional-reminder-requested"
    reason = (prefix + "This work turn has no verified closeout checkpoint. Writes or meaningful dialogue need real RAM; "
              "file finished stages through normal workflow closeout now. Use context closeout_debt=\"" + scope +
              "\"; include the native log when available. For a blocker use " + _runtime_command() + " assess --scope " +
              scope + " --disposition blocked --reason-file PATH --root ROOT (use disposition noop for a trivial exchange). "
              "Queued means submitted, not landed. "
              "A registered checkpoint is not a completed native final. Skip denied steps, report them and continue independent authorized work; "
              "never route around a denial.")
    return {"decision": "block", "reason": reason + (" " + reminder if reminder else "")}, "continuation-requested"


def publish(root, context, result, values):
    """Workflow root owns evidence; explicit debt root owns the hook scope."""
    work_root = Path(root).resolve()
    if Path(context["root"]).resolve() != work_root:
        raise ValueError("workflow evidence root differs from context")
    selected = context.get("closeout_debt_root", str(work_root))
    if not isinstance(selected, str) or not selected or len(selected) > 2048 or not Path(selected).is_absolute():
        raise ValueError("explicit closeout debt root requires an absolute checkout path")
    root = Path(selected).resolve()
    evidence = _evidence_binding(root, work_root) if root != work_root else None
    scope = context["closeout_debt"]
    with _mutation(root, scope, scope):
        if _uncertain(root, scope):
            raise ValueError("uncertain checkpoint cannot publish verification")
        return _publish(root, context, result, values, work_root, evidence)


def _publish(root, context, result, values, work_root, evidence):
    """Workflow-only producer. An unbound green result cannot discharge a turn."""
    scope = context["closeout_debt"]
    record = load(root, scope)
    from . import companion_capture as capture_api
    native = None
    if record.get("native_log") and not context.get("native_log"):
        raise ValueError("recorded native log requires matching workflow capture context")
    if context.get("native_log"):
        if not record.get("native_log") or Path(record["native_log"]).resolve() != Path(context["native_log"]).resolve():
            raise ValueError("native log differs from hook identity")
        native = capture_api.native_identity(context["native_log"], stage="pre-final")
        active = _read(_active_path(root, {k: record["scope"][k] for k in ("app", "checkout_root", "session_id", "agent_id")}))[0]
        if (active.get("scope_id") != scope or native["turn_id"] != active.get("event_turn") or
                (record["scope"]["agent_id"] is None and native["thread_id"] != record["scope"]["session_id"])):
            raise ValueError("current native turn differs from hook scope")
    else:
        if record["scope"].get("agent_id") is not None:
            raise ValueError("child registered identity unavailable; exact native proof required")
        if context.get("session_id") != record["scope"].get("session_id"):
            raise ValueError("registered checkpoint session differs from hook scope")
    if not isinstance(result.get("closeout_attention"), dict) or result["closeout_attention"].get("required") is not False or result.get("command") != "closeout":
        raise ValueError("workflow closeout is unresolved")
    proof = {"profile": PROOF, "scope": record["scope"], "generation": record["generation"],
             "basis": "native-event" if native else "registered-checkpoint", "counts_completed": False,
             "native": {key: native[key] for key in ("thread_id", "turn_id", "stage")} if native else None}
    receipt = result.get("closeout_receipt", {})
    organization = result.get("organization", {})
    rows = [result.get("write_retry"), organization.get("queue") if isinstance(organization, dict) else None]
    lifecycle_rows = result.get("lifecycle", [])
    if isinstance(lifecycle_rows, list):
        rows.extend(lifecycle_rows)
    pending = [row for row in rows if isinstance(row, dict) and row.get("outcome") in {"queued", "requeued", "already-queued"}]
    if pending:
        request_ids = [row.get("request_id") for row in pending]
        if (receipt.get("state") != "submitted" or not all(isinstance(item, str) and item for item in request_ids)
                or not set(request_ids) <= set(receipt.get("request_ids", []))):
            raise ValueError("durable submission receipt unavailable")
        proof.update(state="submitted", request_ids=request_ids)
        if len(request_ids) > 16:
            raise ValueError("closeout request metadata exceeds cap")
    else:
        memory = result.get("memory", result)
        if not isinstance(memory, dict):
            raise ValueError("workflow memory result unavailable")
        terminal = memory.get("result", {})
        saved = memory.get("capture_receipt", {})
        if (not isinstance(terminal, dict) or not isinstance(saved, dict)
                or memory.get("outcome") not in {"completed", "already-completed"} or terminal.get("current_verified") is not True):
            raise ValueError("native workflow capture/readback unavailable")
        capture = saved.get("receipt")
        if native and (saved.get("outcome") not in {"recorded", "idempotent"} or saved.get("readback") != capture
                       or not isinstance(capture, dict) or capture.get("thread_id") != native["thread_id"]
                       or capture.get("turn_id") != native["turn_id"]):
            raise ValueError("workflow capture/readback belongs to another native turn")
        lifecycle = result.get("lifecycle")
        if lifecycle is not None:
            if not isinstance(lifecycle, list) or not lifecycle or len(lifecycle) > 16 or any(not isinstance(row, dict) or row.get("outcome") != "complete" or not row.get("result") for row in lifecycle):
                raise ValueError("required work filing has no terminal readback")
        if isinstance(values, dict) and isinstance(values.get("values"), dict):
            values = values["values"]
        if lifecycle is None and not (isinstance(values, dict) and values.get("work_closeout") == [] and isinstance(values.get("work_closeout_reason"), str)
                  and values["work_closeout_reason"].strip()):
            raise ValueError("explicit work filing/no-change assessment required")
        if native:
            capture_path = Path(saved["path"]).resolve().relative_to(work_root).as_posix()
            persisted, capture_meta = _file_proof(work_root, capture_path, MAX_CAPTURE)
            if persisted != capture:
                raise ValueError("persisted native capture receipt differs")
            proof["capture"] = capture_meta
        identity = memory["operation"]["identity"]
        if (not isinstance(identity, dict) or identity.get("session_id") != context.get("session_id")
                or identity.get("work_id") != context.get("work_id") or identity.get("issuer_id") != context.get("actor")):
            raise ValueError("RAM owner differs from workflow context")
        request = {"profile": "eutonos.tokenslang.ram-read.v1", "operation": "get", "identity": identity,
                   "revision": terminal["readback"]["revision"], "max_bytes": 32768, "limit": 1}
        ram_ref = capture_api.verified_ram_reference(capture_api.native_ram_readback(work_root, request), request)
        if native and ram_ref != capture["ram"]:
            raise ValueError("current RAM differs from capture reference")
        _, ram_meta = _file_proof(work_root, ram_ref["path"], MAX_RAM)
        proof.update(state="verified-prefinal", ram=ram_meta, ram_identity=identity, ram_revision=ram_ref["revision"])
    # Refuse publication if another observed tool crossed the producer boundary.
    if load(root, scope)["generation"] != record["generation"]:
        raise ValueError("observed work changed during closeout verification")
    if evidence is not None:
        proof["evidence"] = evidence
        record["evidence"] = evidence
    else:
        record.pop("evidence", None)
    _write(_path(root, scope), record)
    _write(_path(root, scope, ".proof.json"), proof)
    state = _verified(root, record)
    return {"outcome": state, "scope_id": scope, "basis": proof["basis"], "counts_completed": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Explicit bounded closeout acknowledgement; never fabricates RAM")
    parser.add_argument("--root", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    ack = sub.add_parser("assess")
    ack.add_argument("--scope", required=True)
    ack.add_argument("--disposition", choices=("noop", "blocked"), required=True)
    ack.add_argument("--reason-file", required=True)
    ack.add_argument("--root", default=argparse.SUPPRESS)
    try:
        args = parser.parse_args(argv)
        with Path(args.reason_file).open("rb") as stream:
            reason = stream.read(4001)
        if len(reason) > 4000:
            raise ValueError("assessment reason file exceeds byte cap")
        result = assess(args.root, args.scope, args.disposition, reason.decode("utf-8"))
        print(json.dumps(result)); return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"outcome": "unverified", "message": str(exc)})); return 2


if __name__ == "__main__":
    raise SystemExit(main())
