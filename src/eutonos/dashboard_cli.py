"""Local EUTONOS dashboard with a narrow analytics tracking setting."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import parse_qs, urlparse

from . import dashboard, dashboard_features, tokenslang as ts

# Largest unread POST body read before an early refusal; anything bigger is
# abusive for a localhost settings endpoint and just gets the connection closed.
POST_DRAIN_LIMIT = 1 << 20

# The Features page's writes (one path per switch); added to the POST allowlist in do_POST.
FEATURE_POSTS = tuple("/api/v1/features/" + name for name in dashboard_features.ACTIONS)


def _model_watch_hint(root=None, *, scan=False):
    """Optional global metadata cannot break a selected-root dashboard read."""
    try:
        from . import model_watch
        outcome = None
        if scan:
            outcome = model_watch.scan_if_due(root=root)
        hint = model_watch.pickup_hint()
        if isinstance(outcome, dict) and outcome.get("status") == "attention-required":
            return {**(hint or {}), "error": "Watch metadata unavailable"}
        return hint
    except Exception:  # isolate corrupt/busy optional evidence, omit private errors
        return {"pending_count": 0, "sources": {}, "error": "Watch metadata unavailable"}


def parser():
    value = argparse.ArgumentParser(description="Serve a read-only EUTONOS dashboard.")
    value.add_argument("command", nargs="?", choices=("serve", "snapshot"), default="serve")
    value.add_argument("--root", action="append", default=[], help="Selected EUTONOS repository root; repeat for more.")
    value.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"))
    value.add_argument("--port", type=int, default=8765)
    return value


def app(roots):
    selected = tuple(Path(p).resolve(strict=True) for p in roots)
    settings_token = secrets.token_urlsafe(32)
    ram_searches = {}
    ram_search_lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "EUTONOSDashboard/1"

        def _send(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store" if content_type.startswith("application/json") else "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            request_host = (self.headers.get("Host") or "").split(":", 1)[0].lower()
            if request_host not in {"127.0.0.1", "localhost"}:
                return self._send(403, b'{"outcome":"denied","message":"Dashboard accepts local hosts only."}', "application/json; charset=utf-8")
            try:
                if path == "/api/v1/snapshot":
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if (not set(query) <= {"refresh_state", "project", "view", "source", "detail"}
                            or any(len(v) != 1 for v in query.values())
                            or query.get("refresh_state", ["1"]) != ["1"]
                            or query.get("detail", ["full"]) not in (["full"], ["summary"])):
                        raise ValueError("Invalid snapshot query")
                    project = int(query.get("project", ["0"])[0])
                    if selected and not 0 <= project < len(selected):
                        raise ValueError("Invalid selected project index")
                    view, source = query.get("view", ["current"])[0], query.get("source", [None])[0]
                    if view not in {"current", "main", "checkout", "source"} or (view == "source") != bool(source):
                        raise ValueError("Invalid work view/source")
                    watch = _model_watch_hint(selected[0] if selected else None, scan=True)
                    snapshot = dashboard.snapshot(selected, refresh_state="refresh_state" in query,
                                                  work_view=view, work_source=source, work_project=project,
                                                  detail=query.get("detail", ["full"])[0])
                    snapshot["model_watch"] = watch
                    snapshot["settings_token"] = settings_token
                    body = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    return self._send(200, body, "application/json; charset=utf-8")
                if path == "/api/v1/module":
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if (set(query) not in ({"project", "name"}, {"project", "name", "refresh_state"})
                            or any(len(v) != 1 for v in query.values())
                            or query.get("refresh_state", ["1"]) != ["1"]
                            or query["name"][0] not in dashboard.LAZY_MODULES):
                        raise ValueError("Invalid module query")
                    project = int(query["project"][0])
                    if not 0 <= project < len(selected):
                        raise ValueError("Invalid selected project index")
                    modules = dashboard.lazy_module(selected[project], query["name"][0],
                                                    refresh_state="refresh_state" in query)
                    body = json.dumps({"project": project, "name": query["name"][0], "modules": modules},
                                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    return self._send(200, body, "application/json; charset=utf-8")
                if path == "/api/v1/current-work":
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if (not {"project"} <= set(query) or not set(query) <= {"project", "view", "source", "refresh", "knowledge"}
                            or any(len(v) != 1 for v in query.values())):
                        raise ValueError("Invalid Current Work query")
                    project = int(query["project"][0])
                    if not 0 <= project < len(selected):
                        raise ValueError("Invalid selected project index")
                    view, source = query.get("view", ["current"])[0], query.get("source", [None])[0]
                    if (view not in {"current", "main", "checkout", "source"}
                            or (view == "source") != bool(source)
                            or query.get("refresh", ["1"]) != ["1"]
                            or query.get("knowledge", ["1"]) not in (["0"], ["1"])):
                        raise ValueError("Invalid work view/source")
                    result = dashboard.current_view(selected[project], view=view, source=source,
                                                    refresh="refresh" in query,
                                                    include_knowledge=query.get("knowledge", ["1"]) == ["1"])
                    return self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                                      "application/json; charset=utf-8")
                if path == "/api/v1/model-watch/catalog":
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if set(query) != {"project"} or len(query["project"]) != 1:
                        raise ValueError("Catalog requires one selected project index")
                    project = int(query["project"][0])
                    if not 0 <= project < len(selected):
                        raise ValueError("Invalid selected project index")
                    from . import model_catalog
                    catalog = model_catalog.describe_catalog(selected[project])
                    result = {"models": [name for name, item in catalog["models"].items()
                                         if item.get("available") is not False],
                              "model_catalog": catalog, "model_watch": _model_watch_hint()}
                    return self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                                      "application/json; charset=utf-8")
                if path in {"/api/v1/skills/search", "/api/v1/skills/read"}:
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    expected = {"project", "query", "tool", "status", "offset"} if path.endswith("search") else {"project", "id", "version", "sha256"}
                    if set(query) != expected or any(len(v) != 1 for v in query.values()):
                        raise ValueError("Invalid selected skill query")
                    project = int(query["project"][0])
                    if not 0 <= project < len(selected):
                        raise ValueError("Invalid selected project index")
                    from . import skills
                    if path.endswith("search"):
                        result = skills.search(selected[project], query["query"][0], tool=query["tool"][0] or None,
                                               status=query["status"][0] or None, limit=20, offset=int(query["offset"][0]))
                    else:
                        result = skills.get(selected[project], query["id"][0], version=query["version"][0], sha256=query["sha256"][0])
                    return self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
                if path in {"/api/v1/ram/search", "/api/v1/ram/get"}:
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    required = {"project", "query", "offset"} if path.endswith("search") else {"project", "path", "revision", "sha256"}
                    allowed = required | {"view", "source"} | ({"snapshot"} if path.endswith("search") else set())
                    if not required <= set(query) or not set(query) <= allowed or any(len(v) != 1 for v in query.values()):
                        raise ValueError("Invalid selected RAM query")
                    project = int(query["project"][0])
                    if not 0 <= project < len(selected):
                        raise ValueError("Invalid selected project index")
                    from . import ram_relevance
                    view = query.get("view", ["current"])[0]
                    source = query.get("source", [None])[0] or None
                    if view not in {"current", "main", "checkout", "source", "local"}:
                        raise ValueError("Invalid RAM view")
                    if path.endswith("search"):
                        snapshot_id = query.get("snapshot", [""])[0]
                        if snapshot_id:
                            with ram_search_lock:
                                cached = ram_searches.get(project)
                            if (cached is None or cached["id"] != snapshot_id
                                    or cached["query"] != query["query"][0]
                                    or cached.get("view") != view or cached.get("source") != source):
                                raise ValueError("RAM search snapshot expired; refresh memories.")
                            frozen = cached["value"]
                        else:
                            frozen = ram_relevance.search_snapshot(selected[project], query["query"][0], explicit=True,
                                                                   view=view, source=source)
                            snapshot_id = secrets.token_urlsafe(18)
                            with ram_search_lock:
                                # The dashboard needs only the current browser snapshot for each project.
                                ram_searches[project] = {"id": snapshot_id, "query": query["query"][0], "value": frozen,
                                                        "view": view, "source": source}
                        result = ram_relevance.snapshot_page(frozen, limit=50, offset=int(query["offset"][0]))
                        result["snapshot"] = snapshot_id
                    else:
                        result = ram_relevance.get(selected[project], query["path"][0], int(query["revision"][0]),
                                                   query["sha256"][0], explicit=True, view=view, source=source)
                    return self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
                if path == "/api/v1/documents/get":
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if not {"project", "path", "source", "sha256"} <= set(query) or not set(query) <= {"project", "path", "source", "sha256", "offset", "max_bytes"} or any(len(v) != 1 for v in query.values()):
                        raise ValueError("Exact document source, path and hash required")
                    project = int(query["project"][0])
                    if not 0 <= project < len(selected):
                        raise ValueError("Invalid selected project index")
                    from . import current_work, current_work_knowledge
                    inventory = current_work.sources(selected[project])
                    result = current_work_knowledge.read_document(selected[project], inventory["sources"],
                        path=query["path"][0], source_id=query["source"][0], sha256=query["sha256"][0],
                        offset=int(query.get("offset", ["0"])[0]), max_bytes=int(query.get("max_bytes", ["131072"])[0]))
                    return self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
                if path == '/api/participant-profile':
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if set(query) != {'project'} or len(query['project']) != 1:
                        raise ValueError('Profile requires one selected project index')
                    project = int(query['project'][0])
                    if not 0 <= project < len(selected): raise ValueError('Invalid selected project index')
                    from .work_attribution import profile_status
                    return self._send(200, json.dumps(profile_status(selected[project]), ensure_ascii=False).encode(), 'application/json; charset=utf-8')
                if path == "/api/v1/settings":
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if set(query) != {"project"} or len(query["project"]) != 1:
                        raise ValueError("Settings require one selected project index")
                    project = int(query["project"][0])
                    if not 0 <= project < len(selected):
                        raise ValueError("Invalid selected project index")
                    from .settings import default_brief_template, read_hooks, read_roles, status
                    from . import doctor
                    result = status(selected[project], project)
                    roles = read_roles(selected[project])
                    hooks_panel = read_hooks(selected[project])
                    result["roles"] = roles["roles"]
                    result["roles_revision"] = roles["revision"]
                    # Owner decision (2026-09-29): "roles" above stays the raw saved
                    # value the edit form binds to and writes back verbatim -- never
                    # inject the built-in default text into it, or saving would bake
                    # the default into the file. This sibling map lets the dashboard
                    # show "(default)" next to a role whose saved brief_template is
                    # empty, without touching what gets written on save.
                    result["role_brief_template_defaults"] = {name: default_brief_template(name)
                                                              for name in roles["roles"]}
                    result["hooks"] = hooks_panel["hooks"]
                    result["hooks_revision"] = hooks_panel["revision"]
                    result["model_watch"] = _model_watch_hint()
                    result["install_doctor"] = doctor.report(root=selected[project])
                    body = json.dumps(result, ensure_ascii=False,
                                      separators=(",", ":")).encode("utf-8")
                    return self._send(200, body, "application/json; charset=utf-8")
                if path == "/api/v1/hooks/recent":
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if not {"project"} <= set(query) or not set(query) <= {"project", "limit"} or any(len(v) != 1 for v in query.values()):
                        raise ValueError("Invalid hooks recent-events query")
                    project = int(query["project"][0])
                    if not 0 <= project < len(selected):
                        raise ValueError("Invalid selected project index")
                    limit = int(query.get("limit", ["100"])[0])
                    if not 1 <= limit <= 500:
                        raise ValueError("Invalid hooks recent-events limit")
                    from . import role_hooks
                    result = {"events": role_hooks.read_recent_events(selected[project], limit=limit)}
                    body = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    return self._send(200, body, "application/json; charset=utf-8")
                if path == "/api/v1/crosstalk/status":
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if set(query) != {"project"} or len(query["project"]) != 1:
                        raise ValueError("Invalid crosstalk query")
                    project = int(query["project"][0])
                    if not 0 <= project < len(selected):
                        raise ValueError("Invalid selected project index")
                    from . import crosstalk_dashboard_feeds as ct_feeds
                    body = json.dumps(ct_feeds.status(selected[project]), ensure_ascii=False,
                                      separators=(",", ":")).encode("utf-8")
                    return self._send(200, body, "application/json; charset=utf-8")
                if path == "/api/v1/companion/metrics":
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    allowed = {"project", "amount", "unit", "from", "to"}
                    if not query or not set(query) <= allowed or any(len(values) != 1 for values in query.values()):
                        raise ValueError("Invalid metrics range query")
                    if set(query) not in ({"project"}, {"project", "amount", "unit"}, {"project", "from", "to"}):
                        raise ValueError("Invalid metrics range query")
                    project = int(query["project"][0])
                    if not 0 <= project < len(selected):
                        raise ValueError("Invalid selected project index")
                    from .metric_index import project as companion_project
                    from .companion_metrics import window
                    result = window(companion_project(selected[project]), project,
                                    amount=query.get("amount", [None])[0], unit=query.get("unit", [None])[0],
                                    from_value=query.get("from", [None])[0], to_value=query.get("to", [None])[0])
                    return self._send(200, json.dumps(result, ensure_ascii=False,
                                                      separators=(",", ":")).encode("utf-8"),
                                      "application/json; charset=utf-8")
                if path == "/api/v1/benchmark-report":
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if set(query) != {"project", "report_path", "report_sha256"} or any(len(values) != 1 for values in query.values()):
                        raise ValueError("Benchmark reports require one project and one pinned report reference")
                    project = int(query["project"][0])
                    if not 0 <= project < len(selected):
                        raise ValueError("Invalid selected project index")
                    from .companion_benchmarks import read_report
                    body = read_report(selected[project], query["report_path"][0], query["report_sha256"][0]).encode("utf-8")
                    return self._send(200, body, "text/plain; charset=utf-8")
                if path == "/api/v1/surface-image":
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if set(query) != {"project", "name"} or any(len(values) != 1 for values in query.values()):
                        raise ValueError("Surface images require one project and one retained image name")
                    project = int(query["project"][0])
                    if not 0 <= project < len(selected):
                        raise ValueError("Invalid selected project index")
                    body, mime = dashboard.surface_image(selected[project], query["name"][0])
                    return self._send(200, body, mime)
                if path == "/api/v1/features":
                    # The Features page: every part and setting with its own outcome. Works with no project (a fresh
                    # machine) and with EUTONOS not installed; `project` is only needed for the RAM settings.
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if not set(query) <= {"project"} or any(len(v) != 1 for v in query.values()):
                        raise ValueError("Invalid features query")
                    root = None
                    if "project" in query:
                        project = int(query["project"][0])
                        if selected and not 0 <= project < len(selected):
                            raise ValueError("Invalid selected project index")
                        root = dashboard_features.project_root(selected, project)
                    body = json.dumps(dashboard_features.read(root), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    return self._send(200, body, "application/json; charset=utf-8")
                if path == "/api/v1/offers":
                    # EUTONOS's own lists, read from the installed package; no project and no query involved.
                    if parsed.query:
                        return self._send(400, b'{"outcome":"invalid-request","message":"This list takes no query."}',
                                          "application/json; charset=utf-8")
                    try:
                        body = json.dumps(dashboard.offers(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    except ts.ReadError as exc:
                        return self._send(500, json.dumps({"outcome": "unavailable", "message": exc.message}).encode("utf-8"),
                                          "application/json; charset=utf-8")
                    return self._send(200, body, "application/json; charset=utf-8")
                asset = "index.html" if path in ("/", "/index.html") else path.removeprefix("/")
                content = dashboard.static(asset)
                mime = {"index.html": "text/html; charset=utf-8", "app.css": "text/css; charset=utf-8", "brand.css": "text/css; charset=utf-8",
                        "mobile.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "appearance.js": "text/javascript; charset=utf-8",
                        "companion.css": "text/css; charset=utf-8", "companion.js": "text/javascript; charset=utf-8",
                        "user_tests.js": "text/javascript; charset=utf-8",
                        "repo_views.js": "text/javascript; charset=utf-8", "repo_views.css": "text/css; charset=utf-8",
                        "eutonos-epsilon-32.png": "image/png", "eutonos-epsilon-64.png": "image/png"}[asset]
                return self._send(200, content, mime)
            except Exception as exc:
                body = json.dumps({"outcome": "error", "message": str(exc)}).encode("utf-8")
                if path in {"/api/v1/skills/search", "/api/v1/skills/read"}:
                    invalid = isinstance(exc, (ValueError, TypeError, KeyError)) or getattr(exc, "code", None) in {"invalid", "unsupported"}
                    return self._send(400 if invalid else 404, b'{"outcome":"unavailable","message":"Skill request unavailable or source changed; refresh the catalog."}', "application/json; charset=utf-8")
                if path in {"/api/v1/ram/search", "/api/v1/ram/get"}:
                    invalid = isinstance(exc, (ValueError, TypeError, KeyError)) or getattr(exc, "code", None) in {"invalid", "unsupported", "wrong-scope", "incomplete"}
                    return self._send(400 if invalid else 404, b'{"outcome":"invalid-ram-request"}' if invalid else b'{"outcome":"unavailable","message":"The exact memory is unavailable or changed; search again."}', "application/json; charset=utf-8")
                if path == "/api/v1/benchmark-report":
                    return self._send(400 if isinstance(exc, (ValueError, TypeError, KeyError)) else 404,
                                      b'{"outcome":"invalid-benchmark-report"}', "application/json; charset=utf-8")
                if (path in ({"/api/participant-profile", "/api/v1/settings", "/api/v1/model-watch/catalog", "/api/v1/hooks/recent", "/api/v1/companion/metrics",
                             "/api/v1/ram/search", "/api/v1/ram/get", "/api/v1/module", "/api/v1/crosstalk/status", "/api/v1/features"})
                        and isinstance(exc, (ValueError, TypeError, KeyError))):
                    return self._send(400, b'{"outcome":"invalid-settings"}', "application/json; charset=utf-8")
                return self._send(404 if path != "/api/v1/snapshot" else 500, body, "application/json; charset=utf-8")

        def _refuse(self, status, body):
            """Reply early to a POST whose body was not read. Windows resets a
            socket closed with unread bytes, and the client then loses the reply
            (WinError 10053), so read a bounded body first."""
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                size = 0
            if 0 < size <= POST_DRAIN_LIMIT:
                self.rfile.read(size)
            self.close_connection = True
            return self._send(status, body, "application/json")

        def do_POST(self):
            # Only the selected repository's analytics switch is mutable.
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin", "")
            if (host.split(":", 1)[0].lower() not in {"127.0.0.1", "localhost"}
                    or origin != "http://" + host
                    or self.headers.get("X-Eutonos-Settings") != settings_token):
                return self._refuse(403, b'{"outcome":"denied"}')
            if self.path not in ("/api/v1/companion/tracking", "/api/v1/settings", "/api/v1/crosstalk/action", "/api/v1/model-watch/ack", "/api/participant-profile") + FEATURE_POSTS:
                return self._refuse(404, b'{"outcome":"not-found"}')
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 8192 or self.headers.get("Content-Type") != "application/json":
                    return self._refuse(400, b'{"outcome":"invalid-settings"}')
                value = json.loads(self.rfile.read(size))
                if self.path == '/api/participant-profile':
                    if (not isinstance(value, dict) or not {'project','given_name','family_name','expected_revision'} <= set(value)
                            or not set(value) <= {'project','given_name','family_name','expected_revision','participant_id'}
                            or type(value['project']) is not int or not 0 <= value['project'] < len(selected)):
                        raise ValueError('Invalid selected profile project')
                    from .work_attribution import configure_profile
                    from .settings import StaleSettings
                    try:
                        result = configure_profile(selected[value['project']], {k:v for k,v in value.items() if k not in {'project','expected_revision'}}, value['expected_revision'])
                    except StaleSettings as exc:
                        return self._send(409, json.dumps({'outcome':'conflict', **exc.current},ensure_ascii=False).encode(), 'application/json; charset=utf-8')
                    return self._send(200, json.dumps(result,ensure_ascii=False).encode(), 'application/json; charset=utf-8')
                if self.path in FEATURE_POSTS:
                    # The install toggles. Same guard as every write here (Host and Origin, X-Eutonos-Settings token,
                    # the allowlist above); each action is one switch and the owner's own answer comes back as is.
                    name = self.path.rsplit("/", 1)[1]
                    if not isinstance(value, dict):
                        raise ValueError("Invalid features request")
                    root = None
                    if name == "ram":
                        index = value.pop("project", None)
                        if type(index) is not int or (selected and not 0 <= index < len(selected)):
                            raise ValueError("Invalid selected project index")
                        root = dashboard_features.project_root(selected, index)
                    result = dashboard_features.act(name, value, root)
                    return self._send(200, json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                                      "application/json; charset=utf-8")
                if self.path == "/api/v1/model-watch/ack":
                    if (not isinstance(value, dict) or set(value) != {"ids"} or
                            not isinstance(value["ids"], list) or not 1 <= len(value["ids"]) <= 16 or
                            any(not isinstance(identity, str) or not 1 <= len(identity) <= 128
                                for identity in value["ids"])):
                        raise ValueError("Review requires explicit shown change IDs")
                    from . import model_watch
                    try:
                        reviewed = model_watch.ack(ids=value["ids"])
                    except Exception:
                        reviewed = {"status": "attention-required"}
                    result = {"status": reviewed.get("status"), "model_watch": _model_watch_hint()}
                    return self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                                      "application/json; charset=utf-8")
                if self.path == "/api/v1/settings":
                    if (not isinstance(value, dict) or set(value) != {"project", "revision", "section", "value"}
                            or type(value["project"]) is not int or not 0 <= value["project"] < len(selected)):
                        raise ValueError("Invalid settings request")
                    from .settings import StaleSettings, update, update_hooks, update_roles
                    root = selected[value["project"]]
                    try:
                        if value["section"] == "roles":
                            result = update_roles(root, value["value"], expected_revision=value["revision"])
                        elif value["section"] == "hooks":
                            result = update_hooks(root, value["value"], expected_revision=value["revision"])
                        else:
                            result = update(root, value["project"], value["revision"],
                                            value["section"], value["value"])
                    except StaleSettings as exc:
                        body = json.dumps({"outcome": "stale-settings", "settings": exc.current},
                                          ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                        return self._send(409, body, "application/json; charset=utf-8")
                    return self._send(200, json.dumps(result, ensure_ascii=False,
                                                      separators=(",", ":")).encode("utf-8"),
                                      "application/json; charset=utf-8")
                if self.path == "/api/v1/crosstalk/action":
                    if (not isinstance(value, dict) or "project" not in value
                            or type(value["project"]) is not int or not 0 <= value["project"] < len(selected)
                            or "action" not in value):
                        raise ValueError("Invalid crosstalk action request")
                    from . import crosstalk_dashboard_feeds as ct_feeds
                    payload = {k: v for k, v in value.items() if k != "project"}
                    result = ct_feeds.action(selected[value["project"]], payload)
                    # A semantic failure is {"ok": false, ...} over a normal 200 so the
                    # panel can show it inline.
                    return self._send(200, json.dumps(result, ensure_ascii=False,
                                                       separators=(",", ":")).encode("utf-8"),
                                      "application/json; charset=utf-8")
                if set(value) != {"project", "enabled"} or type(value["project"]) is not int or type(value["enabled"]) is not bool or not 0 <= value["project"] < len(selected):
                    raise ValueError("Invalid tracking settings")
                from .companion_history import set_tracking
                result = set_tracking(selected[value["project"]], value["enabled"])
                return self._send(200, json.dumps(result).encode(), "application/json")
            except (ValueError, TypeError, KeyError, ts.ReadError) as exc:
                if self.path == '/api/participant-profile':
                    return self._send(400, json.dumps({'outcome':'error','message':str(exc)},ensure_ascii=False).encode(), 'application/json; charset=utf-8')
                return self._send(400, b'{"outcome":"invalid-settings"}', "application/json")

        def log_message(self, fmt, *args):
            pass

    return Handler


def main(argv=None):
    args = parser().parse_args(argv)
    roots = args.root or [str(Path.cwd())]
    if args.command == "snapshot":
        print(json.dumps(dashboard.snapshot(roots), ensure_ascii=False, indent=2))
        return 0
    server = ThreadingHTTPServer((args.host, args.port), app(roots))
    print(f"EUTONOS dashboard: http://{args.host}:{server.server_port}", flush=True)
    print("Selected roots (native records read-only): " + ", ".join(str(Path(p).resolve()) for p in roots), flush=True)

    def refresh_metric_indexes():
        # Keep the dashboard available while its derived metrics cache refreshes.
        from .metric_index import update as update_metric_index
        for root in roots:
            try:
                indexed = update_metric_index(root)
                print("Metric index: " + indexed["outcome"] + " (" + str(indexed["runs"]) + " recorded runs)", flush=True)
                for advisory in indexed.get("advisories", []):
                    print("Metric index warning: " + advisory["message"], flush=True)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                print("Metric index warning: " + str(exc) + "; dashboard startup continued.", flush=True)

    Thread(target=refresh_metric_indexes, name="eutonos-metric-index", daemon=True).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
