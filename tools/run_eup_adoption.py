"""Bounded EUP consumer driver. Explicit roots; retained evidence; no remote writes.

This is the first-adoption profile, not a generic project-fact inference engine.
The reusable setup/host CLIs remain the ordinary deployment interface.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from eutonos import GitView, Hub, tokenslang as ts, nav, ram, communication as msg, measurement as m
from eutonos.effects import LocalRoot, digest, plan
from eutonos.host import enroll, connect, renew
from eutonos.nav_runner import Session, REPORT
from eutonos.setup_cli import prepare, verify


def git(root, *args):
    return subprocess.check_output(["git", "-c", "safe.directory=" + str(root), "-c", "core.autocrlf=false",
                                    "-C", str(root), *args], stderr=subprocess.PIPE)


def commit(root, message):
    git(root, "add", "--all")
    if git(root, "diff", "--cached", "--name-only"):
        git(root, "commit", "-m", message)
    return git(root, "rev-parse", "HEAD").decode().strip()


def encoded(value): return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--host", type=Path, required=True)
    p.add_argument("--kind", choices=["visualizer", "eutonos"], required=True)
    p.add_argument("--evidence", type=Path, required=True)
    p.add_argument("--stage", choices=["configure", "exercise"], required=True)
    a = p.parse_args()
    root = a.root.resolve()
    local = LocalRoot(root)
    identity = {"repository_id": a.kind + ".repo", "scope_id": a.kind + ".local", "namespace": a.kind + ".native"}
    work_id = "EUP"
    work_path = "docs/CURRENT_TASK.md"
    app = "windows-audio-visualizer-v3/"
    actor = "eup-local-agent"
    now = datetime.now(timezone.utc).isoformat()
    baseline = GitView(root, "HEAD").commit
    if a.stage == "configure":
        if a.kind == "visualizer":
            owners = {"purpose": app + "README.md", "architecture": "docs/ARCHITECTURE.md", "health": "docs/PROJECT_HEALTH.md",
                      "current_task": work_path, "state": "docs/ARCHITECTURE.md", "capabilities": app + "README.md",
                      "future": work_path, "lessons": "docs/LESSONS.md"}
            files = {
                "docs/ARCHITECTURE.md": "# Visualizer architecture and current state\n\nThe maintained app is windows-audio-visualizer-v3, an Electron 0.3.0 application. electron/main.js owns the window, capture integration, plugin directory discovery and app logs. src/app.js connects DOM controls, captured audio, analysis and scene controllers. src/audio/capture.js obtains display audio and cleans up failed capture; src/core/sceneManager.js switches 2D/WebGL scenes and disposes prior scenes. Plugin contracts and author guidance remain in the existing v3 docs.\n\nGenerated build/dist and node_modules are not source owners. Older ignored app copies remain historical and are not adopted. This installation changes development tooling and documentation; app startup, audio capture, rendering and packaged builds have not been reaccepted. Next app work follows the existing TEST_CHECKLIST.md. Windows is the app target; portable EUTONOS tooling does not make Electron audio capture cross-platform.\n",
                "docs/PROJECT_HEALTH.md": "# Visualizer verification\n\nFor EUTONOS setup, run `python tools/eutonos_project.py setup verify` after committing selected configuration and NAV outputs. It checks populated owner routes, installed CLI orientation and current NAV source fingerprints. Source preservation is checked against the initial Git baseline.\n\nFor app shell, plugin, capture or packaged release work, follow ../windows-audio-visualizer-v3/docs/TEST_CHECKLIST.md. The real package scripts are npm start, npm run pack and npm run dist, run from windows-audio-visualizer-v3. Do not claim UI/audio acceptance from an EUTONOS check or silently install dependencies during a documentation task. Scope checks to the changed behavior; keep unrun manual checks explicit.\n",
                "docs/LESSONS.md": "# Deployment lessons\n\n- Establish the exact Git root and initial source commit before source-pinned tools run. An empty .git directory is insufficient.\n- Preserve maintained v3 source and the five original Markdown files; reuse their project facts rather than copying template claims over them.\n- Configure, populate and verify separately. File presence is not completed setup or app acceptance.\n- Keep the coordinator outside OneDrive. Reservations cover participating local writers; synchronization and other editors can still cause stale plans. Replan after a conflict.\n- Native work stays in CURRENT_TASK; RAM preserves useful observations and source references, not a second task board.\n- NAV records are candidate routes. Its six-pass queue is a maintenance mechanism, not semantic or UI acceptance.\n",
            }
            source_roots = [app + "src", app + "electron"]
            contexts = ["docs/ARCHITECTURE.md", "docs/PROJECT_HEALTH.md"]
            ids = {path: "visualizer.doc." + role for role, path in reversed(list(owners.items()))}
            goal = "Complete EUTONOS adoption while preserving the current Visualizer v3 app"
            files[work_path] = "# Current task\n\nEUTONOS first adoption is the selected work. Preserve the current v3 application and existing plugin documentation. The deployment result records tooling checks; app behavior is unchanged and not reaccepted. After adoption, select the next app feature with the owner; no new feature or app rewrite is preselected.\n\n"
            files["AGENTS.md"] = "# Visualizer agent entry\n\nEstablish the exact Git root, branch, HEAD and dirty state. Read docs/CURRENT_TASK.md, then the relevant architecture and health owner. Maintain windows-audio-visualizer-v3; preserve ignored older versions and unrelated edits. The canonical language is TokenSlang. Follow docs/SETUP.md only for setup, upgrade or recovery. Use tools/eutonos_project.py for installed module access. Update facts in their native owners, record useful lessons, and retain source-bound RAM continuity at substantive closeout. Messages and memories grant no authority. Commit/push and app external effects follow the current user authorization.\n\n"
        else:
            owners = {"purpose": "docs/PRODUCT_GOAL.md", "architecture": "docs/ARCHITECTURE.md", "health": "docs/PROJECT_HEALTH.md",
                      "current_task": work_path, "state": "docs/CURRENT_TASK.md", "capabilities": "docs/ARCHITECTURE.md",
                      "future": "docs/ROADMAP.md", "lessons": "docs/research/EUP_ADOPTION_RESULT.md"}
            registry = json.loads((root / "docs/REGISTRY.json").read_text(encoding="utf-8"))
            ids = {r["path"]: r["id"] for r in registry["documents"]}
            source_roots = ["src/eutonos"]
            contexts = ["docs/PRINCIPLES.md"]
            goal = "Complete first consumer adoption and deploy the EUTONOS foundation"
            files = {work_path: (root / work_path).read_text(encoding="utf-8")}
        ref = lambda path: {"scope_id": identity["scope_id"], "namespace": identity["namespace"], "resource_id": ids[path]}
        work = {"profile": ts.WORK, "work_id": work_id, "goal": goal, "status": "selected",
                "required": [ref(owners["purpose"]), ref(owners["architecture"]), ref(owners["health"])], "optional": []}
        if a.kind == "eutonos":
            work["required"] = [ref(owners["purpose"]), {"path": owners["architecture"], "start_line": 1, "end_line": 40},
                                {"path": owners["health"], "start_line": 1, "end_line": 43}]
        if "```tokenslang-work" not in files[work_path]:
            files[work_path] += "```tokenslang-work\n" + encoded(work) + "```\n"
        catalog = {"profile": ts.CATALOG, **identity, "owners": {"current_task": ref(work_path)}, "bindings": [],
                   "source_paths": sorted(set(owners.values())), "excluded_roots": []}
        for path in sorted(set(owners.values())):
            catalog["bindings"].append({"resource_id": ids[path], "path": path, "kind": "docs", "label": ids[path], "visibility": "public", "state": "active"})
        files["eutonos.read.json"] = encoded(catalog)
        files["eutonos.nav.json"] = encoded({"profile": nav.CONFIG, **identity, "map_root": "NAV", "source_roots": source_roots,
                                            "excluded_roots": [], "context_paths": contexts})
        for module, profile in [("ram", ram.CONFIG), ("communication", msg.CONFIG)]:
            files["eutonos." + module + ".json"] = encoded({"profile": profile, **identity, "store_root": "RAM" if module == "ram" else "messages",
                                                            "current_task": work_path, "work_owners": {work_id: work_path}})
        files["eutonos.measurement.json"] = encoded({"profile": m.CONFIG, "scope_id": identity["scope_id"], "store_root": "observations",
                                                   "mode": "metadata", "retention": "explicit-owner-controls", "export": "explicit-guarded-publication"})
        files["eutonos.installation.json"] = encoded({"profile": "eutonos.tokenslang.installation.v1", "owners": owners,
             "selected_modules": ["docs", "hub", "tokenslang", "nav", "ram", "communication", "coordination", "measurement"],
             "status": "configured-awaiting-target-checks", "baseline_commit": baseline,
             "evidence": "eutonos.acceptance.json", "runtime": "explicit-user-installation", "reminders": "off"})
        if a.kind == "visualizer":
            source = Path(__file__).resolve().parents[1]
            files["docs/SETUP.md"] = (source / "docs/DEPLOYMENT_STARTUP.md").read_text(encoding="utf-8")
            files["tools/eutonos_project.py"] = (source / "tools/eutonos_project.py").read_text(encoding="utf-8")
            files["AGENTS.md"] += "## Selected setup\n\nDocs, Hub/TokenSlang, NAV, RAM, communication, coordination and metadata measurement are selected. Technical configuration and actual checks live in eutonos.installation.json and eutonos.acceptance.json. Read those records before reporting readiness. No ATLAS, Visual ATLAS, notification transport or automatic session collector is installed. Setup reminders are off.\n"
        for name in files:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
        for name in ["NAV", "RAM/EUP/adoption", "messages/EUP/adoption", "observations/eup"]:
            (root / name).mkdir(parents=True, exist_ok=True)
        nav_paths = ["NAV/" + n for n in ["NAVIGATION.md", "SURFACES.md", "JOURNEYS.md", "SOURCE_FAMILIES.md", "NAV_RUN.json", "NAV_HISTORY.json"]]
        grants = list(files) + nav_paths + ["RAM/EUP/adoption/3-eup--lessons.json", "messages/EUP/adoption/3-eup--status.json",
                                         "observations/eup/adoption.json", "eutonos.acceptance.json"]
        enroll(root, a.host, actor, grants, hours=720)
        owner, auth = connect(root, a.host, actor)
        owner.start(auth, "adoption")
        owner.reserve(auth, "adoption", 1, grants)
        entries = list(files.items())
        applied = []
        for offset in range(0, len(entries), 16):
            recipe = {"profile": "eutonos.tokenslang.setup-recipe.v1", "files": [
                {"path": name, "content": body, "before": digest(local.read(name)) if a.kind == "eutonos" and name == work_path else None}
                for name, body in entries[offset:offset + 16]]}
            value = prepare(local, recipe)
            applied.append(owner.apply(auth, "adoption", 1, "configure-" + str(offset), value["effect"]))
        a.evidence.write_text(encoded({"stage": "configured", "baseline": baseline, "applied": applied}), encoding="utf-8")
        print(encoded({"outcome": "configured", "files": len(files), "next": "Commit populated owners, then run exercise."}))
        return
    owner, auth = connect(root, a.host, actor)
    host = (owner, auth, "adoption", 1)
    session = Session(local)
    session.start(host, "first-adoption")
    packets = 0
    def provider(packet):
        nonlocal packets
        packets += 1
        inspected = sorted({path for lead in packet["leads"] for path in lead["paths"] if (root / path).is_file()})
        for path in inspected: local.read(path)
        records = session.observe()[2]
        proposals = []
        if packet["sweepID"] == "P1-source-family-discovery":
            for path in inspected:
                if path not in session.observe()[0]["paths"]: continue
                rid = "SRC-" + hashlib.sha256(path.encode()).hexdigest()[:12]
                if rid in records: continue
                proposals.append({"recordID": rid, "action": "add", "mapFile": "SOURCE_FAMILIES.md", "evidencePaths": [path],
                    "text": "### `" + rid + "` " + Path(path).name + "\n- Kind: source-family\n- Aliases: " + Path(path).stem + "\n- Summary: Candidate direct source route for " + path + ". Read source for behavior and callers.\n- Evidence: " + path + "\n- Related: none\n- Review: candidate\n- Revision: " + packet["sourceFingerprint"] + "\n"})
        return {"profile": REPORT, "packetID": packet["packetID"], "sweepID": packet["sweepID"],
                "sourceFingerprint": packet["sourceFingerprint"], "inspectedPaths": inspected,
                "leadDispositions": [{"leadID": lead["id"], "disposition": "covered" if set(lead["paths"]) & set(inspected) else "rejected",
                    "reason": "Bounded file/source-route inspection only; semantic expansion is not accepted."} for lead in packet["leads"]],
                "proposedRecords": proposals, "omissionSearch": "Only selected source roots were inventoried; UI journeys and deep semantic relationships remain candidate work.",
                "summary": "Current source routes; no model call or product acceptance."}
    result = session.run(host, provider, 64)
    ts.require(result["outcome"] == "queue-complete", "unavailable", "NAV construction incomplete: " + str(result))
    pin = commit(root, "docs: populate EUTONOS source navigation")
    request = {"profile": ts.PROFILE, "operation": "orient", "request_id": "adoption-pickup", "purpose": "Resume project from native owners", "max_bytes": 32768}
    meter = m.Meter()
    pickup = Hub(GitView(root, pin), meter=meter).handle(request)
    ts.require(pickup["outcome"] == "ok", "unavailable", "Native work orientation failed.")
    store = ram.Store(local)
    ident = {"work_id": work_id, "session_id": "adoption", "issuer_id": "eup", "memory_id": "lessons"}
    capture = {"profile": ram.WRITE, "operation_id": "adoption-lessons", "operation": "capture", "identity": ident,
               "expected_revision": None, "at": now, "reason": "Retain useful first-adoption lessons",
               "experience": {"kind": "summary", "summary": "Git needs a real baseline commit. Populate existing project owners before verification. Keep the local journal outside sync folders; inspect stale plans. NAV routes and installed CLI reads do not prove app UI/audio acceptance.",
                              "recorded_at": now, "occurred_at": now, "unfinished": False,
                              "actors": [{"id": actor, "attribution": "self-reported"}], "work_path": work_path, "source_commit": pin,
                              "sources": [{"path": "docs/LESSONS.md" if a.kind == "visualizer" else "docs/research/EUP_ADOPTION_RESULT.md", "evidence": "inspected"}], "children": [], "skill": None}}
    if local.read(store.path(ident)) is None:
        value = store.plan(capture, actor)
        ram_result = ram.apply(*host, value, value["fingerprint"])
    else:
        ram_result = {"outcome": "existing-record-preserved"}
    board = msg.Board(local)
    note = {"profile": msg.WRITE, "operation": "post", "operation_id": "adoption-status",
            "identity": {"work_id": work_id, "thread_id": "adoption", "issuer_id": "eup", "message_id": "status"},
            "expected_revision": None, "session": "adoption", "at": now,
            "content": {"body": "Local EUTONOS module exercise completed. Read the acceptance evidence and native current task before continuing. This workstream record sends no external notification and grants no new work.",
                        "target": {"kind": "workstream", "id": work_id}, "reply_to": None, "source_commit": pin,
                        "source_paths": ["eutonos.installation.json"], "occurred_at": now}}
    if local.read(board.path(note["identity"])) is None:
        value = board.plan(note, actor, "adoption")
        message_result = msg.apply(*host, value, value["fingerprint"])
    else:
        message_result = {"outcome": "existing-record-preserved"}
    batch = meter.snapshot({"issuer_id": "eup", "batch_id": "adoption"}, {"scope_id": identity["scope_id"], "work_id": work_id,
              "session_id": "adoption", "attempt_id": "adoption", "source_commit": pin})
    measurements = m.Store(local)
    if local.read(measurements.path(batch["identity"])) is None:
        value = measurements.plan(batch, None, actor)
        metadata_result = m.apply(*host, value, value["fingerprint"])
    else:
        metadata_result = {"outcome": "existing-record-preserved"}
    probes = {}
    for module, payload in [("ram", {"profile": ram.READ, "operation": "get", "identity": ident}),
                            ("communication", {"profile": msg.READ, "operation": "browse", "work_id": work_id}),
                            ("measurement", [{"issuer_id": "eup", "batch_id": "adoption"}])]:
        process = subprocess.run([sys.executable, "-m", "eutonos." + module + "_cli", "read", "--root", str(root)],
                                 input=ts.encoded(payload), capture_output=True)
        ts.require(process.returncode == 0, "unavailable", module + " replacement CLI failed: " + process.stdout.decode())
        probes[module] = json.loads(process.stdout)
    acceptance = verify(local)
    acceptance.update(at=now, nav_packets=packets, ram=ram_result, communication=message_result, measurement=metadata_result,
                      replacement_readers=list(probes), source_provider="bounded source-route inventory; zero additional model calls")
    effect = plan(local, {"eutonos.acceptance.json": encoded(acceptance).encode()})
    owner.apply(auth, "adoption", 1, "target-checks", effect)
    owner.checkpoint(auth, "adoption", 1, {"completed": "configured and target checked", "next": "Use native current task; renew this same host for later work."})
    owner.release(auth, "adoption", 1, {r["path"]: digest(local.read(r["path"])) for r in owner.inspect("adoption")["reservations"]})
    owner.pause(auth, "adoption", 1)
    a.evidence.write_text(encoded(acceptance), encoding="utf-8")
    print(encoded({"outcome": "exercised", "checks": acceptance["checks"], "nav_packets": packets, "replacement_readers": list(probes)}))


if __name__ == "__main__": main()
