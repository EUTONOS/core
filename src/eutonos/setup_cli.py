"""Preservation-first project setup with authored recipes and file coordination."""
import argparse
import json
from pathlib import Path
import subprocess
from .hidden_process import no_window
import sys

from . import GitView, Hub, tokenslang as ts, nav, ram, communication, measurement
from . import adoption, boards
from .effects import LocalRoot, digest, plan
from .host import connect, selected_apply
from .nav_runner import Session


def prepare(root, recipe, inputs=()):
    """Zero-write plan. Existing bytes require an explicit expected fingerprint."""
    ts.shape(recipe, {"profile", "files"})
    ts.require(recipe["profile"] == "eutonos.tokenslang.setup-recipe.v1", "unsupported", "Unknown setup recipe.")
    GitView(root.root, "HEAD")
    updates = {}
    for item in ts.sequence(recipe["files"]):
        ts.shape(item, {"path", "content", "before"})
        name = item["path"]
        ts.require(name not in updates and isinstance(item["content"], str), "invalid", "Duplicate or invalid recipe file.")
        current = root.read(name)
        desired = item["content"].encode("utf-8")
        ts.require(current == desired or digest(current) == item["before"], "conflict", "Preserve existing owner: " + name)
        updates[name] = desired
    effect = plan(root, updates, inputs=inputs)
    return {"profile": "eutonos.tokenslang.setup-plan.v1", "effect": effect,
            "fingerprint": digest(ts.encoded(effect)), "paths": sorted(updates),
            "qualification": "Authored content; file presence is not acceptance."}


def verify(root):
    view = GitView(root.root, "HEAD")
    # Read the pinned Git inputs before waiting for independent live pickup.
    index = nav.NavIndex(view).handle({"profile": nav.READ, "operation": "browse", "limit": 64})
    ts.require(index["outcome"] == "ok" and index.get("items"), "unavailable", "NAV has no readable routes.")
    manifest = ts.loads(root.read("eutonos.installation.json").decode("utf-8"))
    ts.require(manifest["profile"] == "eutonos.tokenslang.installation.v1", "unsupported", "Unknown installation.")
    required = {"purpose", "architecture", "health", "current_task", "state", "capabilities", "future", "lessons"}
    ts.require(required <= manifest["owners"].keys(), "invalid", "Missing core owner evaluation.")
    owners = {}
    for role, path in manifest["owners"].items():
        body = root.read(path)
        ts.require(body and len(body.strip()) > 30, "missing", "Populate the native owner for " + role)
        owners[role] = {"path": path, "sha256": digest(body)}
    local_boards = boards.project(root)
    ts.require(local_boards["status"] in {"available", "not-configured"}, "unavailable",
               "Local native board owners are unreadable or incomplete.")
    # The workflow entry supports native boards and legacy tokenslang-work owners.
    # A fresh isolated process proves installed pickup, not AI comprehension.
    process = subprocess.run([sys.executable, "-I", "-B", "-m", "eutonos.workflow_cli", "pickup", "--root", str(root.root)],
                             capture_output=True, timeout=45, **no_window())
    ts.require(process.returncode == 0, "unavailable", "Installed workflow CLI pickup failed.")
    pickup = ts.loads(process.stdout.decode("utf-8"))
    ts.require(pickup["outcome"] == "pickup", "unavailable", "Native work is not orientable.")
    maintenance = Session(root).maintenance()
    ts.require(maintenance["outcome"] != "no-baseline", "missing", maintenance.get("next", "NAV has no baseline run."))
    ts.require(maintenance["outcome"] == "no-op", "stale", "NAV needs source maintenance.")
    ram.Store(root)
    communication.Board(root)
    measurement.Store(root)
    return {"outcome": "verified", "commit": view.commit, "owners": owners, "nav_records": len(index["items"]),
            "checks": ["populated owner routes", "fresh installed workflow CLI", "committed NAV browse", "NAV freshness",
                       "RAM/communication/measurement configuration"],
            "limits": ["No independent AI comprehension test", "No application UI/audio acceptance", "Cooperative local host only",
                       "Workflow pickup reads live native owners; commit is the verification baseline"]}


def _install_check(args) -> int:
    """Run `<checkout>/tools/install_check.py`, which needs a git checkout (it builds the wheel from HEAD)."""
    import importlib.util
    script = Path(args.root).resolve() / "tools" / "install_check.py"
    if not script.is_file():
        print(json.dumps({"outcome": "error", "error": "install-check needs a EUTONOS checkout: no tools/install_check.py "
                          f"under {Path(args.root).resolve()} (pass --root CHECKOUT)"}), file=sys.stderr)
        return 2
    spec = importlib.util.spec_from_file_location("eutonos_install_check", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main(["--root", str(Path(args.root).resolve())] + (["--real-venv"] if args.real_venv else [])
                       + (["--keep"] if args.keep else []))


def main(argv=None):
    p = argparse.ArgumentParser(description="EUTONOS guided setup: inspect, profile, plan, apply, verify.")
    p.add_argument("command", choices=["inspect", "profile", "discover", "plan", "plan-profile",
                                       "apply", "verify", "verify-profile",
                                       "prereq", "add-project", "hosts-report", "install-check", "remove-project", "finalize"])
    p.add_argument("--root", default=".")
    p.add_argument("--request")
    p.add_argument("--host", help="add-project: adopt this existing host (a path, or hosts/NAME under the EUTONOS home)")
    p.add_argument("--actor")
    p.add_argument("--attempt")
    p.add_argument("--generation", type=int, default=1)
    p.add_argument("--fingerprint")
    p.add_argument("--effect-id")
    # install-check (IPC5 lane A): build, install, verify, uninstall and reinstall on temporary folders only
    p.add_argument("--real-venv", action="store_true", help="install-check: build the runtime as a real venv (no index)")
    p.add_argument("--keep", action="store_true", help="install-check: keep the temporary tree and print its path")
    # remove-project (IPC5 lane D): detach by default; --de-adopt --plan PLAN.json for full de-adoption; --dry-run reads only
    p.add_argument("--de-adopt", action="store_true", help="remove-project: also reverse eutonos.adoption.json (needs --plan)")
    p.add_argument("--plan", help="remove-project: preservation plan JSON, every adoption-manifest path keep or remove")
    p.add_argument("--dry-run", action="store_true", help="remove-project: print every planned change and change nothing")
    p.add_argument("--no-permissions", action="store_true", help="add-project: do not apply the project permission entries")
    args = p.parse_args(argv)
    if args.command == "install-check":
        return _install_check(args)
    if args.command == "remove-project":
        from . import repository_lifecycle
        forwarded = ["--root", args.root] + (["--host", args.host] if args.host else []) + (["--de-adopt"] if args.de_adopt else [])             + (["--plan", args.plan] if args.plan else []) + (["--dry-run"] if args.dry_run else [])
        return repository_lifecycle.main(forwarded)
    try:
        if args.command in ("prereq", "hosts-report"):
            # Neither needs a project; both only report and install nothing.
            if args.command == "prereq":
                from . import prereq
                result = prereq.install_prereq_check()
                parts = prereq.unavailable_without_git(result, prereq.parts_needing_git())
                result["unavailable_without_git"] = [item["id"] for item in parts]
                if result["missing"] or parts:
                    print(chr(10).join(prereq.install_prereq_lines(result, parts)), file=sys.stderr)
                sys.stdout.buffer.write(ts.encoded(result) + b"\n")
                return 2 if result["missing"] else 0
            from . import projects
            sys.stdout.buffer.write(ts.encoded(projects.hosts_report()) + b"\n")
            return 0
        if args.command == "add-project":
            from . import repository_lifecycle
            result = repository_lifecycle.add_project(args.root, args.actor, args.host, apply_permissions=not args.no_permissions)
            sys.stdout.buffer.write(ts.encoded(result) + b"\n")
            return 2 if result["outcome"] == "ambiguous" or "partly" in result.get("permissions", {}).get("message", "") else 0
        if args.command == "finalize":
            from . import setup_finalize
            sys.stdout.buffer.write(ts.encoded(setup_finalize.finalize_plan(args.root)) + b"\n")
            return 0
        root = LocalRoot(args.root)
        if args.command == "profile":
            result = adoption.profile_description()
        elif args.command == "discover":
            result = adoption.discover(root)
        elif args.command == "inspect":
            view = GitView(root.root, "HEAD")
            result = {"outcome": "inspected", "root": str(root.root), "commit": view.commit,
                      "classification": "existing-installation" if root.read("eutonos.installation.json") else "existing-project",
                      "adoption": adoption.discover(root),
                      "next": "Read native owners; select an explicit adoption profile or prepare an authored setup recipe. The setup guide (docs/SETUP.md) ships only in a EUTONOS source checkout; installed, `eutonos-setup profile` prints the profile schema and the `add-repository` checklist (eutonos/checklists/add-repository.md) gives the order."}
        elif args.command == "verify":
            result = verify(root)
        else:
            if args.request:
                with Path(args.request).open("rb") as stream:
                    raw = stream.read()
            else:
                raw = sys.stdin.buffer.read()
            value = ts.loads(raw.decode("utf-8"))
            if args.command == "plan":
                result = prepare(root, value)
            elif args.command == "plan-profile":
                result = adoption.plan_profile(root, value)
            elif args.command == "verify-profile":
                result = adoption.verify_profile(root, value)
            else:
                coordinator, authority = connect(root.root, ts.string(args.host), args.actor)
                ts.require(value["profile"] == "eutonos.tokenslang.setup-plan.v1", "unsupported", "Expected setup plan.")
                result = selected_apply(coordinator, authority, ts.string(args.attempt), args.generation,
                                        ts.string(args.effect_id), value["effect"], ts.string(args.fingerprint))
        sys.stdout.buffer.write(ts.encoded(result) + b"\n")
        return 2 if result.get("outcome") == "conflict" else 0
    except (ts.ReadError, OSError, UnicodeError, KeyError, TypeError) as exc:
        sys.stdout.buffer.write(ts.encoded({"outcome": "error", "code": getattr(exc, "code", "invalid"),
                                          "message": getattr(exc, "message", "Setup input or local I/O unavailable.")}) + b"\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
