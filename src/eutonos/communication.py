"""Work-linked records, not a transport, task dispatcher or authorization owner."""
from datetime import datetime
import re

from . import tokenslang as ts
from .effects import digest, plan, validate_plan
from .source import GitView, safe_path

CONFIG = "eutonos.tokenslang.communication-config.v1"
RECORD = "eutonos.tokenslang.message.v1"
WRITE = "eutonos.tokenslang.communication-write.v1"
READ = "eutonos.tokenslang.communication-read.v1"
PLAN = "eutonos.tokenslang.communication-plan.v1"


def decode(body):
    ts.require(body is not None, "missing", "Communication source is absent.")
    try:
        return ts.loads(body.decode("utf-8"))
    except UnicodeError as exc:
        raise ts.ReadError("unsupported", "Communication requires UTF-8 JSON.") from exc


def stamp(value):
    try:
        moment = datetime.fromisoformat(ts.string(value))
    except ValueError as exc:
        raise ts.ReadError("invalid", "Invalid communication time.") from exc
    ts.require(moment.utcoffset() is not None, "invalid", "Communication time requires an offset.")


def slug(value):
    ts.require(isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value)),
               "invalid", "Expected a bounded identity component.")
    return safe_path(value)


class Board:
    def __init__(self, root, config_path="eutonos.communication.json", denied_paths=()):
        self.root, self.config_path = root, safe_path(config_path)
        self.denied_paths = set(denied_paths)
        ts.require(config_path not in self.denied_paths, "denied", "Communication config is not public.")
        self.config_bytes = root.read(config_path)
        self.cfg = decode(self.config_bytes)
        ts.shape(self.cfg, {"profile", "repository_id", "scope_id", "namespace", "store_root", "current_task", "work_owners"})
        ts.require(self.cfg["profile"] == CONFIG, "unsupported", "Unknown communication config.")
        for key in ("repository_id", "scope_id", "namespace"):
            ts.string(self.cfg[key])
        safe_path(self.cfg["store_root"]); safe_path(self.cfg["current_task"])
        ts.require(isinstance(self.cfg["work_owners"], dict) and 0 < len(self.cfg["work_owners"]) <= 64,
                   "invalid", "Explicit native work bindings are required.")
        for key, value in self.cfg["work_owners"].items():
            slug(key); safe_path(value)
        ts.require(not any(self.inside(p) for p in [config_path, self.cfg["current_task"], *self.cfg["work_owners"].values()]),
                   "invalid", "Work/configuration must remain outside communication output.")

    def inside(self, path):
        return path == self.cfg["store_root"] or path.startswith(self.cfg["store_root"] + "/")

    def path(self, identity):
        ts.shape(identity, {"work_id", "thread_id", "issuer_id", "message_id"})
        for part in identity.values():
            slug(part)
        return safe_path(self.cfg["store_root"] + "/" + identity["work_id"] + "/" + identity["thread_id"] + "/"
                         + str(len(identity["issuer_id"])) + "-" + identity["issuer_id"] + "--" + identity["message_id"] + ".json")

    def observer(self):
        observed, budget = {}, [0, 0]
        def read(path):
            body = self.root.read(path)
            budget[0] += 1; budget[1] += len(body or b"")
            ts.require(budget[0] <= 256 and budget[1] <= 4194304 and len(observed.keys() | {path}) <= 64,
                       "overflow", "Communication read bound exhausted; narrow work/thread or use exact identity.")
            ts.require(path not in observed or observed[path] == digest(body), "stale", "Communication source changed during observation.")
            observed[path] = digest(body)
            return body
        ts.require(read(self.config_path) == self.config_bytes, "stale", "Communication configuration changed.")
        return observed, read

    def load(self, path, read):
        ts.require(self.inside(safe_path(path)), "wrong-scope", "Message is outside the selected store.")
        record = decode(read(path))
        ts.shape(record, {"profile", "scope_id", "namespace", "identity", "revision", "visibility", "state", "versions", "events", "receipts"})
        ts.require(record["profile"] == RECORD and self.path(record["identity"]) == path
                   and all(record[k] == self.cfg[k] for k in ("scope_id", "namespace")), "wrong-scope", "Message identity/view differs.")
        ts.require(type(record["revision"]) is int and record["revision"] >= 1, "invalid", "Invalid message revision.")
        ts.require(record["visibility"] in ("public", "private") and record["state"] in ("active", "suppressed", "archived"),
                   "unsupported", "Unsupported message controls.")
        ts.require(isinstance(record["receipts"], dict) and len(record["receipts"]) <= 64, "invalid", "Message receipt bound exceeded.")
        ts.require(isinstance(record["versions"], list) and 0 < len(record["versions"]) <= 32, "invalid", "Message history bound exceeded.")
        for version in record["versions"]:
            ts.shape(version, {"revision", "body", "sender", "target", "reply_to", "work", "selection", "sources", "occurred_at", "recorded_at"})
            ts.require(isinstance(version["body"], str) and 0 < len(version["body"].encode()) <= 8192, "invalid", "Message body exceeds 8 KiB.")
            stamp(version["occurred_at"]); stamp(version["recorded_at"])
            ts.shape(version["sender"], {"actor", "session", "attempt", "attribution", "model"})
            for key in ("actor", "session", "attempt"):
                ts.string(version["sender"][key])
            ts.shape(version["target"], {"kind", "id"})
            ts.require(version["target"]["kind"] in ("workstream", "agent"), "unsupported", "Unknown target kind.")
            ts.string(version["target"]["id"])
            if version["reply_to"] is not None:
                self.path(version["reply_to"])
            ts.shape(version["work"], {"path", "commit", "sha256"})
            ts.shape(version["selection"], {"path", "sha256"})
            safe_path(version["work"]["path"]); safe_path(version["selection"]["path"])
            for source in ts.sequence(version["sources"]):
                ts.shape(source, {"path", "commit", "sha256"})
                safe_path(source["path"])
        for event in ts.sequence(record["events"]):
            ts.shape(event, {"kind", "actor", "session", "attempt", "at", "message_revision", "basis"})
            ts.require(event["kind"] in ("posted", "delivered", "acknowledged", "resolved"), "unsupported", "Unknown observation kind.")
            stamp(event["at"]); ts.string(event["basis"])
        return record

    def plan(self, request, actor, attempt):
        ts.shape(request, {"profile", "operation", "operation_id", "identity", "expected_revision", "session", "at"},
                 {"content", "observation", "controls"})
        ts.require(request["profile"] == WRITE, "unsupported", "Unsupported communication write profile.")
        operation = request["operation"]
        ts.require(operation in ("post", "correct", "observe", "control"), "unsupported", "Unsupported communication operation.")
        ts.require(("content" in request) == (operation in ("post", "correct"))
                   and ("observation" in request) == (operation == "observe") and ("controls" in request) == (operation == "control"),
                   "invalid", "Fields do not match the selected operation.")
        for value in (actor, attempt, request["operation_id"], request["session"]):
            ts.string(value)
        stamp(request["at"])
        expected = request["expected_revision"]
        ts.require(expected is None or type(expected) is int and expected >= 1, "invalid", "Invalid expected revision.")
        observed, read = self.observer()
        path = self.path(request["identity"])
        previous = read(path)
        record = self.load(path, read) if previous else None
        fingerprint = digest(ts.encoded([request, actor, attempt]))
        updates = {}
        if record and request["operation_id"] in record["receipts"]:
            ts.require(record["receipts"][request["operation_id"]] == fingerprint, "conflict", "Logical message operation already has different content.")
        else:
            ts.require((record is None) == (operation == "post") and expected == (record["revision"] if record else None),
                       "conflict", "Message exists or its expected revision changed.")
            if not record:
                record = {"profile": RECORD, "scope_id": self.cfg["scope_id"], "namespace": self.cfg["namespace"],
                          "identity": request["identity"], "revision": 1, "visibility": "public", "state": "active",
                          "versions": [], "events": [], "receipts": {}}
            else:
                record["revision"] += 1
            if operation in ("post", "correct"):
                content = request["content"]
                ts.shape(content, {"body", "target", "reply_to", "source_commit", "source_paths", "occurred_at"})
                ts.require(isinstance(content["body"], str) and 0 < len(content["body"].encode()) <= 8192, "invalid", "Message exceeds 8 KiB.")
                ts.shape(content["target"], {"kind", "id"})
                ts.require(content["target"]["kind"] in ("workstream", "agent"), "unsupported", "Unsupported target.")
                ts.string(content["target"]["id"]); stamp(content["occurred_at"])
                ts.require(content["target"]["kind"] != "workstream" or content["target"]["id"] == request["identity"]["work_id"],
                           "wrong-scope", "Workstream target differs from the record owner.")
                work_path = self.cfg["work_owners"].get(request["identity"]["work_id"])
                ts.require(work_path is not None, "missing", "Native work owner is not mapped.")
                ts.require(read(work_path) is not None, "missing", "Native work owner is absent.")
                task = read(self.cfg["current_task"])
                ts.require(task is not None, "missing", "Current Task is absent.")
                view = GitView(self.root.root, content["source_commit"])
                ts.require(view.commit == content["source_commit"], "invalid", "Message evidence needs an exact source commit.")
                work, _ = view.read(work_path)
                sources = []
                for source in ts.sequence(content["source_paths"]):
                    body, _ = view.read(source)
                    sources.append({"path": source, "commit": view.commit, "sha256": digest(body)})
                if content["reply_to"] is not None:
                    parent = self.load(self.path(content["reply_to"]), read)
                    ts.require(parent["state"] == "active" and parent["visibility"] == "public"
                               and all(parent["identity"][k] == request["identity"][k] for k in ("work_id", "thread_id")),
                               "denied", "Reply parent is ineligible or belongs to another work/thread.")
                record["versions"].append({"revision": record["revision"], "body": content["body"], "target": content["target"],
                    "reply_to": content["reply_to"], "sender": {"actor": actor, "session": request["session"], "attempt": attempt,
                                                               "attribution": "host-supplied", "model": None},
                    "work": {"path": work_path, "commit": view.commit, "sha256": digest(work)}, "sources": sources,
                    "selection": {"path": self.cfg["current_task"], "sha256": digest(task)},
                    "occurred_at": content["occurred_at"], "recorded_at": request["at"]})
                if operation == "post":
                    record["events"].append({"kind": "posted", "actor": actor, "session": request["session"], "attempt": attempt,
                                             "at": request["at"], "message_revision": 1, "basis": "Guarded local publication; transport delivery unobserved."})
            elif operation == "observe":
                observation = request["observation"]
                ts.shape(observation, {"kind", "message_revision", "basis"})
                ts.require(observation["kind"] in ("delivered", "acknowledged", "resolved"), "unsupported", "Unsupported delivery/disposition observation.")
                ts.require(observation["message_revision"] == record["versions"][-1]["revision"], "stale", "Observation refers to superseded message content.")
                ts.string(observation["basis"])
                record["events"].append({**observation, "actor": actor, "session": request["session"], "attempt": attempt, "at": request["at"]})
            else:
                ts.shape(request["controls"], {"visibility", "state"})
                ts.require(request["controls"]["visibility"] in ("public", "private")
                           and request["controls"]["state"] in ("active", "suppressed", "archived"), "unsupported", "Unknown message controls.")
                record.update(request["controls"])
            record["receipts"][request["operation_id"]] = fingerprint
            body = ts.encoded(record) + b"\n"
            ts.require(len(body) <= ts.MAX_SOURCE_BYTES and len(record["versions"]) <= 32 and len(record["events"]) <= 64
                       and len(record["receipts"]) <= 64, "overflow", "Message history is full; preserve it and open a new record.")
            updates[path] = body
        effect = plan(self.root, updates, inputs=observed)
        ts.require(all(effect["observed"][p] == h for p, h in observed.items()), "stale", "Message inputs changed while planning.")
        validate_plan(self.root, effect)
        return {"profile": PLAN, "actor": actor, "attempt": attempt, "operation_id": request["operation_id"],
                "record_path": path, "effect": effect, "fingerprint": digest(ts.encoded(effect))}

    def read(self, request):
        ts.shape(request, {"profile", "operation"}, {"identity", "work_id", "thread_id", "agent_id", "include_archived", "max_bytes"})
        ts.require(request["profile"] == READ and request["operation"] in ("get", "browse", "describe"), "unsupported", "Unknown communication read.")
        budget = request.get("max_bytes", 32768)
        ts.require(type(budget) is int and 512 <= budget <= 32768, "invalid", "Communication budget must be 512..32768.")
        ts.require(type(request.get("include_archived", False)) is bool, "invalid", "Archive visibility must be explicit.")
        observed, read = self.observer()
        result = {"profile": READ, "outcome": "ok", "scope_id": self.cfg["scope_id"], "namespace": self.cfg["namespace"],
                  "view": "observed-local-records; later/remote controls unobserved", "authorization": "none; consult native work",
                  "delivery": "attributed observations only; no notification transport"}
        if request["operation"] == "describe":
            result.update(operations=["get", "browse"], dispatch=False)
            ts.require(len(ts.encoded(result)) <= budget, "overflow", "Increase max_bytes for the capability description.")
            return result
        task = read(self.cfg["current_task"])
        ts.require(task is not None and self.cfg["current_task"] not in self.denied_paths, "denied", "Current work source is unavailable.")
        if request["operation"] == "get":
            paths = [self.path(request.get("identity"))]
        else:
            prefix = self.cfg["store_root"]
            if "work_id" in request:
                prefix += "/" + slug(request["work_id"])
                if "thread_id" in request:
                    prefix += "/" + slug(request["thread_id"])
            ts.require("thread_id" not in request or "work_id" in request, "invalid", "Thread browse needs its work scope.")
            view = GitView(self.root.root, "HEAD")
            paths = sorted(set(view._git("ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", ":(literal)" + prefix).decode().split("\0")) - {""})
            ts.require(len(paths) <= 64, "overflow", "Narrow work/thread; communication inventory exceeds 64 records.")
        items = []
        for path in paths:
            if not path.endswith(".json") or read(path) is None:
                continue
            record = self.load(path, read)
            version = record["versions"][-1]
            eligible = record["visibility"] == "public" and record["state"] != "suppressed" and path not in self.denied_paths
            eligible = eligible and not any(p in self.denied_paths for p in [version["work"]["path"], *[s["path"] for s in version["sources"]]])
            if not eligible:
                ts.require(request["operation"] != "get", "denied", "Message is not eligible for this read.")
                continue
            if record["state"] == "archived" and not request.get("include_archived", False):
                ts.require(request["operation"] != "get", "archived", "Request archived history explicitly.")
                continue
            if "agent_id" in request and version["target"] != {"kind": "agent", "id": request["agent_id"]}:
                continue
            changes = [{"path": s["path"], "changed": digest(read(s["path"])) != s["sha256"]} for s in version["sources"]]
            items.append({"identity": record["identity"], "revision": record["revision"], "path": path, "sha256": observed[path],
                          "content": version, "events": record["events"], "source_changes": changes,
                          "work_owner_changed": digest(read(version["work"]["path"])) != version["work"]["sha256"],
                          "current_task_changed": digest(task) != version["selection"]["sha256"], "actionable_authority": False})
        ts.require(all(digest(self.root.read(p)) == h for p, h in observed.items()), "stale", "Message/control view changed during retrieval.")
        result.update(items=items, complete=True)
        if len(ts.encoded(result)) > budget:
            return {"profile": READ, "outcome": "overflow", "complete": False,
                    "continuation": "Narrow work/thread, use exact identity or increase max_bytes; no message was truncated."}
        return result


def apply(coordinator, authority, attempt, generation, value, expected_fingerprint):
    ts.shape(value, {"profile", "actor", "attempt", "operation_id", "record_path", "effect", "fingerprint"})
    ts.require(value["profile"] == PLAN and value["actor"] == authority.actor and value["attempt"] == attempt
               and value["fingerprint"] == expected_fingerprint == digest(ts.encoded(value["effect"])), "conflict", "Communication host/plan mismatch.")
    return coordinator.apply(authority, attempt, generation,
                             "communication-" + digest(ts.encoded([attempt, value["operation_id"], value["fingerprint"]])), value["effect"])
