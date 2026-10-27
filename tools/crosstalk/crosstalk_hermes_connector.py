"""Crosstalk create-hermes connector (slice 3B.14).

Run with the Hermes venv python, isolated:  <hermes-venv>\\Scripts\\python.exe -I -B crosstalk_hermes_connector.py
The request is one JSON object on stdin (nothing secret goes on a command line); the result is exactly one JSON
line on stdout.

What it does, all against the live local `hermes serve` backend (verified in the Hermes source, 2026-10-14):
  1. finds the backend port in <hermes_home>/spawn-ledger.json (purpose "serve"),
  2. reads the session token from `GET /` (the page sets window.__HERMES_SESSION_TOKEN__),
  3. opens ws://127.0.0.1:<port>/api/ws?token=... and waits for the gateway.ready event,
  4. session.create (source desktop, title, optional model / provider / reasoning_effort, idempotency key),
  5. prompt.submit with the first prompt,
  6. stays connected until message.complete / error, or the bounded wait ends (the turn then keeps running
     detached in the backend),
  7. prints the stored session id.

The token lives in memory only. It is never written to a file, log, argv, environment variable or output, and every
error text is scrubbed of it. This module reads no credential file (no auth.json, .env or auth.lock).
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

TAG = "crosstalk.hermes.connector.v1"
HERMES_ID_RE = re.compile(r"\d{8}_\d{6}_[0-9a-f]{6}\Z")
# hermes_constants.VALID_REASONING_EFFORTS plus "none" (parse_reasoning_effort maps "none" to disabled).
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
TOKEN_RE = re.compile(r"__HERMES_SESSION_TOKEN__\s*=\s*(\"(?:[^\"\\]|\\.)*\")")
READY_TIMEOUT_S = 20.0
RPC_TIMEOUT_S = 60.0
HTTP_TIMEOUT_S = 4.0
PING_EVERY_S = 15.0
RECV_SLICE_S = 2.0
DEFAULT_WAIT_S = 300.0
MAX_STDIN = 262144


class ConnectorError(Exception):
    def __init__(self, reason: str, detail: str = "", session_id: str | None = None):
        super().__init__(reason)
        self.reason, self.detail, self.session_id = reason, detail, session_id


def scrub(text, token: str | None) -> str:
    """Remove the token (raw and URL-quoted) from any text that may leave this process."""
    value = str(text)
    if token:
        value = value.replace(token, "[token]")
        quoted = urllib.request.quote(token, safe="")
        if quoted != token:
            value = value.replace(quoted, "[token]")
    return value


def validate_effort(effort):
    if effort is None:
        return None
    if not isinstance(effort, str) or effort not in EFFORTS:
        raise ConnectorError("invalid-effort", "one of " + ",".join(EFFORTS))
    return effort


def ledger_ports(hermes_home: str) -> list[int]:
    """Live-looking `serve` ports of this Hermes home, newest registration first. Only the ledger is read."""
    try:
        rows = json.loads((Path(hermes_home) / "spawn-ledger.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    home = str(Path(hermes_home)).lower()
    found = []
    for row in rows:
        if not isinstance(row, dict) or row.get("purpose") != "serve":
            continue
        port = row.get("port")
        if type(port) is not int or not 0 < port < 65536:
            continue
        if row.get("host") not in ("127.0.0.1", "localhost", "::1", "", None):
            continue
        row_home = row.get("hermes_home")
        if isinstance(row_home, str) and row_home and str(Path(row_home)).lower() != home:
            continue
        registered = row.get("registered_at")
        found.append((registered if isinstance(registered, (int, float)) else 0.0, port))
    found.sort(reverse=True)
    ports = []
    for _, port in found:
        if port not in ports:
            ports.append(port)
    return ports


def http_get_root(port: int) -> str:
    request = urllib.request.Request(f"http://127.0.0.1:{port}/", headers={"Accept": "text/html"})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:  # loopback only
        return response.read(262144).decode("utf-8", "replace")


def token_from_page(html: str) -> str | None:
    match = TOKEN_RE.search(html)
    if not match:
        return None
    try:
        value = json.loads(match.group(1))
    except ValueError:
        return None
    return value if isinstance(value, str) and value else None


def ws_connect(url: str):
    from websockets.sync.client import connect  # imported late: the Hermes venv has it, the caller's python may not
    return connect(url, open_timeout=10, close_timeout=5, max_size=16 * 1024 * 1024, ping_interval=None)


class Rpc:
    """A tiny JSON-RPC client over one gateway WebSocket (newline-free text frames)."""

    def __init__(self, ws, token: str, clock=time.monotonic):
        self.ws, self.token, self.clock = ws, token, clock
        self.next_id = 0
        self.events: list[dict] = []
        self.last_ping = clock()

    def _pump(self, wait: float):
        """Read one frame (or None on a quiet slice); keep the heartbeat going."""
        now = self.clock()
        if now - self.last_ping >= PING_EVERY_S:
            self.last_ping = now
            self.next_id += 1
            self.ws.send(json.dumps({"jsonrpc": "2.0", "id": f"ping-{self.next_id}", "method": "gateway.ping"}))
        try:
            raw = self.ws.recv(timeout=wait)
        except TimeoutError:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            frame = json.loads(raw)
        except ValueError:
            return None
        return frame if isinstance(frame, dict) else None

    def wait_event(self, predicate, timeout: float):
        deadline = self.clock() + timeout
        while True:
            for index, event in enumerate(self.events):
                if predicate(event):
                    del self.events[index]
                    return event
            remaining = deadline - self.clock()
            if remaining <= 0:
                return None
            frame = self._pump(min(RECV_SLICE_S, remaining))
            if frame is not None and frame.get("method") == "event" and isinstance(frame.get("params"), dict):
                self.events.append(frame["params"])

    def call(self, method: str, params: dict, timeout: float = RPC_TIMEOUT_S):
        self.next_id += 1
        rid = self.next_id
        self.ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
        deadline = self.clock() + timeout
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise ConnectorError("rpc-timeout", method)
            frame = self._pump(min(RECV_SLICE_S, remaining))
            if frame is None:
                continue
            if frame.get("id") == rid:
                if isinstance(frame.get("error"), dict):
                    error = frame["error"]
                    raise ConnectorError("rpc-error", f"{method} code={error.get('code')} "
                                         f"{scrub(str(error.get('message') or '')[:160], self.token)}")
                result = frame.get("result")
                return result if isinstance(result, dict) else {}
            if frame.get("method") == "event" and isinstance(frame.get("params"), dict):
                self.events.append(frame["params"])


def create_chat(request: dict, *, http_get=None, connect=None, clock=time.monotonic) -> dict:
    """Run the whole create. Raises ConnectorError; a result is a dict for the single output line.

    http_get / connect default to the module's real functions, looked up at call time."""
    http_get = http_get or http_get_root
    connect = connect or ws_connect
    hermes_home = request.get("hermes_home")
    prompt = request.get("prompt")
    if not isinstance(hermes_home, str) or not hermes_home:
        raise ConnectorError("hermes-home-missing")
    if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt:
        raise ConnectorError("prompt-invalid")
    effort = validate_effort(request.get("effort"))
    wait_s = request.get("wait_s", DEFAULT_WAIT_S)
    if not isinstance(wait_s, (int, float)) or isinstance(wait_s, bool) or not 1 <= wait_s <= 3600:
        raise ConnectorError("wait-invalid")

    token = None
    port = None
    saw_page_without_token = False
    for candidate in ledger_ports(hermes_home):
        try:
            page = http_get(candidate)
        except (OSError, urllib.error.URLError, TimeoutError, ValueError):
            continue  # a stale ledger row: nothing listens there
        found = token_from_page(page)
        if found:
            token, port = found, candidate
            break
        saw_page_without_token = True
    if token is None:
        if saw_page_without_token:
            raise ConnectorError("hermes-token-unavailable", "the backend answered but exposes no session token (gated)")
        raise ConnectorError("hermes-not-running", "no live `hermes serve` backend found in spawn-ledger.json")

    ws = None
    stored = None
    try:
        try:
            ws = connect(f"ws://127.0.0.1:{port}/api/ws?token=" + urllib.request.quote(token, safe=""))
        except Exception as error:
            raise ConnectorError("hermes-ws-connect-failed", type(error).__name__ + ": " + scrub(str(error), token)[:300])
        rpc = Rpc(ws, token, clock)
        if rpc.wait_event(lambda e: e.get("type") == "gateway.ready", READY_TIMEOUT_S) is None:
            raise ConnectorError("hermes-gateway-not-ready")
        params = {"source": "desktop", "idempotency_key": "crosstalk-" + str(request.get("idempotency_key") or "")}
        if not request.get("idempotency_key"):
            del params["idempotency_key"]
        if isinstance(request.get("title"), str) and request["title"].strip():
            params["title"] = " ".join(request["title"].split())[:80]
        if request.get("model"):
            params["model"] = request["model"]
            if request.get("provider"):
                params["provider"] = request["provider"]
        if effort is not None:
            params["reasoning_effort"] = effort
        created = rpc.call("session.create", params)
        sid = created.get("session_id")
        stored = created.get("stored_session_id")
        if not isinstance(sid, str) or not sid or not isinstance(stored, str) or not HERMES_ID_RE.fullmatch(stored):
            raise ConnectorError("create-session-id-missing")
        info = created.get("info") if isinstance(created.get("info"), dict) else {}
        _pre_register(request, stored)
        try:
            submitted = rpc.call("prompt.submit", {"session_id": sid, "text": prompt})
        except ConnectorError as error:
            raise ConnectorError("create-prompt-submit-failed", error.reason + " " + error.detail, stored)
        if submitted.get("status") not in ("streaming", "queued"):
            raise ConnectorError("create-prompt-not-accepted", "status=" + str(submitted.get("status"))[:30], stored)
        done = rpc.wait_event(lambda e: e.get("session_id") == sid and e.get("type") in ("message.complete", "error"),
                              float(wait_s))
        if done is None:
            turn = "running"
            turn_status = None
        elif done.get("type") == "error":
            raise ConnectorError("create-turn-error", scrub(str((done.get("payload") or {}).get("message") or "")[:160], token), stored)
        else:
            turn = "completed"
            payload = done.get("payload") if isinstance(done.get("payload"), dict) else {}
            turn_status = payload.get("status") if isinstance(payload.get("status"), str) else None
            if turn_status not in (None, "complete"):
                raise ConnectorError("create-turn-not-completed", "status=" + turn_status[:30], stored)
        return {"stored_session_id": stored, "turn": turn, "turn_status": turn_status,
                "requested": {"model": request.get("model") or None, "provider": request.get("provider") or None,
                              "effort": effort},
                "reported": {"model": str(info.get("model") or "")[:80] or None,
                             "provider": str(info.get("provider") or "")[:40] or None}}
    except ConnectorError:
        raise
    except Exception as error:  # nothing here may carry the token out
        raise ConnectorError("connector-failed", type(error).__name__ + ": " + scrub(str(error), token)[:300], stored)
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


def _pre_register(request: dict, stored: str) -> None:
    """Best effort: record the creator title in the Crosstalk directory before the first prompt runs, so a letter
    sent in that first turn is labelled with it. A failure here never blocks the create."""
    argv = request.get("pre_register_argv")
    if not (isinstance(argv, list) and argv and all(isinstance(item, str) for item in argv)):
        return
    try:
        subprocess.run(argv + ["--session", stored], capture_output=True, timeout=20, stdin=subprocess.DEVNULL)
    except Exception:
        pass


def main() -> int:
    token_free_failure = {"tag": TAG, "ok": False}
    try:
        raw = sys.stdin.buffer.read(MAX_STDIN + 1)
        if len(raw) > MAX_STDIN:
            raise ConnectorError("stdin-too-large")
        try:
            request = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ConnectorError("stdin-not-json")
        if not isinstance(request, dict):
            raise ConnectorError("stdin-not-object")
        value = dict(create_chat(request), tag=TAG, ok=True)
        code = 0
    except ConnectorError as error:
        value = dict(token_free_failure, reason=error.reason, detail=error.detail[:300])
        if error.session_id:
            value["stored_session_id"] = error.session_id
        code = 1
    except Exception as error:
        value = dict(token_free_failure, reason="connector-exception", detail=type(error).__name__)
        code = 1
    sys.stdout.write(json.dumps(value, sort_keys=True) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
