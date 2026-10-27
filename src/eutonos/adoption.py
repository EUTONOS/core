"""Profile-driven, preservation-first adoption for existing repositories."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

from . import __version__, nav, ram, tokenslang as ts
from .effects import LocalRoot, digest
from .source import GitView, safe_path
from .completion_policy import CONTRACT, instruction_plan, merge_block
from . import rule_index

PROFILE = "eutonos.tokenslang.adoption-profile.v1"
MANIFEST = "eutonos.tokenslang.adoption-manifest.v1"
MANIFEST_PATH = "eutonos.adoption.json"
MODULES = ("docs", "delegation", "dashboard", "ram", "nav")
BLOCK_START = "<!-- eutonos:delegation:start -->"
BLOCK_END = "<!-- eutonos:delegation:end -->"
DELEGATION_BLOCK = """<!-- eutonos:delegation:start -->
## EUTONOS delegation

This repository enables instruction-based delegation through the agent host's
existing worker tools. The repository's native owners, boot protocol, write
coordination, tests, and publication rules remain authoritative. Delegation
does not install EUTONOS services or grant writes, Git publication, deployment,
external effects, credentials, or model upgrades.

For optional orientation, an available EUTONOS source checkout offers
`python tools/workflow.py orient --root ROOT`. In an adopted repository with
an intentionally selected installed runtime and this managed wrapper, use
`python tools/eutonos_project.py workflow orient --root ROOT` instead. Omit
`--request` for cold entry with a 32,768-byte output budget; `workflow describe orient`
describes compatible requests and packets. Inspect source pins,
coverage, privacy and selection gaps before dependent work. The reader may
update its cache and fetch explicitly configured Shared Work refs when due.
If the command/runtime is missing, unavailable or incompatible, continue via
applicable native instructions, selected onboarding and exact native owners;
do not require a packet or a setup loop. Upstream onboarding references below
are guidance, not assumed consumer paths. Packets/receipts do not prove reading,
understanding, permission, identity, setup or acceptance. Supplied caller
identity remains unverified; prior receipt pins are untrusted comparison input.
Returned source pins describe observed inputs and validated exact RAM reads, not
caller ownership or authority.

At pickup, when the managed wrapper exposes settings, run
`python tools/eutonos_project.py settings status` once and honor its effective
routing and task overrides. Otherwise use the policy here without requiring a
missing runtime: adaptive delegation is on and model/effort selection is Auto.
The primary balances expected correctness, total cost, handoff, review, and
latency; prefer a cheaper helper only when it is likely to return the same useful
quality. A user task or session choice overrides the default. Persist a durable
choice only when explicitly requested.

The primary may give one bounded, non-recursive helper a complete assignment.
Moving above the current primary model requires user approval unless already
selected. Continue independent work while approval is pending; if no answer is
given, keep that work on the current primary. Read-only helpers need no write
lease. Writing helpers use the repository's existing coordination for their own
exact scope, preserve unrelated work, run relevant checks, and return source
paths, findings, changes, unresolved risks, and actual closeout. If delegation
is disabled or unavailable, the primary continues normally without a setup loop.

Use `python tools/eutonos_project.py workflow help` once when unfamiliar with
the installed operations, then call known commands directly. For substantive
work, keep the native current-task checkpoint recoverable. Before a substantive
final, use `workflow prepare closeout`, answer its semantic prompts, and use
`workflow run closeout` with the existing actor/work context. Respect effective
RAM capture cadence and retrieval settings; do not manufacture receipt fields.
Quick edits need no campaign or repeated discovery. Exact source and native
owners outrank RAM/NAV summaries. A read-only task never authorizes new writes.

`version status` shows optional project/module versions separately from the
installed EUTONOS runtime. Follow the project's own version convention at useful
milestones; do not bump versions on every tiny edit. Portable reference docs
are upstream guidance, not the consumer's selected work board or acceptance.
<!-- eutonos:delegation:end -->
"""


def _delegation_block(profile):
    return DELEGATION_BLOCK.replace(BLOCK_END, rule_index.render(profile["owners"]) + "\n" + BLOCK_END)


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"


def _text(raw, label):
    try:
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise ts.ReadError("unsupported", label + " must be UTF-8.") from exc


def validate_profile(value):
    ts.shape(value, {"profile", "repository_id", "scope_id", "namespace", "owners", "modules"}, {"nav"})
    ts.require(value["profile"] == PROFILE, "unsupported", "Unsupported adoption profile.")
    for key in ("repository_id", "scope_id", "namespace"):
        ts.string(value[key])
    ts.require(isinstance(value["owners"], dict) and value["owners"] and len(value["owners"]) <= 64
               and "current_task" in value["owners"], "invalid", "Explicit current_task and bounded native owners are required.")
    owners = {}
    resource_paths = {}
    for role, owner in value["owners"].items():
        ts.require(isinstance(role, str) and bool(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", role)),
                   "invalid", "Owner roles use lowercase portable names.")
        ts.shape(owner, {"path", "resource_id"})
        path, resource_id = safe_path(owner["path"]), ts.string(owner["resource_id"])
        ts.require(resource_id not in resource_paths or resource_paths[resource_id] == path,
                   "ambiguous", "One resource identity cannot select competing owner paths.")
        resource_paths[resource_id] = path
        owners[role] = {"path": path, "resource_id": resource_id}
    modules = ts.sequence(value["modules"])
    ts.require(modules and len(set(modules)) == len(modules) and set(modules) <= set(MODULES),
               "invalid", "Select unique supported adoption modules.")
    selected_nav = None
    if value.get("nav") is not None:
        selected_nav = value["nav"]
        ts.shape(selected_nav, {"map_root", "source_roots", "excluded_roots", "context_paths"})
        selected_nav = {key: [safe_path(path) for path in ts.sequence(paths)]
                        if key != "map_root" else safe_path(paths)
                        for key, paths in selected_nav.items()}
        ts.require(selected_nav["source_roots"] and selected_nav["context_paths"],
                   "invalid", "NAV creation requires explicit source roots and context paths.")
        ts.require(not any(nav.under(path, [selected_nav["map_root"]])
                           or nav.under(path, selected_nav["excluded_roots"])
                           for path in selected_nav["context_paths"]),
                   "denied", "NAV context cannot be output or excluded source.")
    result = {"profile": PROFILE, "repository_id": value["repository_id"],
              "scope_id": value["scope_id"], "namespace": value["namespace"],
              "owners": owners, "modules": sorted(modules)}
    if selected_nav is not None:
        result["nav"] = selected_nav
    return result


def profile_description():
    return {"profile": PROFILE, "outcome": "profile", "modules": list(MODULES),
            "schema": {"required": ["profile", "repository_id", "scope_id", "namespace", "owners", "modules"],
                       "owner": {"path": "existing UTF-8 native owner", "resource_id": "stable existing ID"},
                       "nav": {"required_when_creating_nav": ["map_root", "source_roots", "excluded_roots", "context_paths"]}},
            "example": {"profile": PROFILE, "repository_id": "project.repo", "scope_id": "project.local",
                        "namespace": "project.native", "owners": {"current_task": {
                            "path": "WORK.md", "resource_id": "project.doc.current-task"}},
                        "modules": ["docs", "delegation", "dashboard"]},
            "qualification": "Profiles select existing owners and modules; they do not create tasks, hosts, authority, or deployment."}


def _read_json(local, path):
    raw = local.read(path)
    return None if raw is None else ts.loads(_text(raw, path))


def _work(local, path):
    raw = local.read(path)
    ts.require(raw is not None, "missing", "Native owner is absent: " + path)
    try:
        return ts.work(_text(raw, path))
    except ts.ReadError as exc:
        raise ts.ReadError(exc.code, "Native current work needs explicit TokenSlang adaptation: " + exc.message) from exc


def _catalog(profile):
    unique = {}
    for owner in profile["owners"].values():
        unique[(owner["resource_id"], owner["path"])] = owner
    bindings = [{"resource_id": owner["resource_id"], "path": owner["path"], "kind": "docs",
                 "label": owner["resource_id"], "visibility": "public", "state": "active"}
                for owner in unique.values()]
    current = profile["owners"]["current_task"]
    return {"profile": ts.CATALOG, "repository_id": profile["repository_id"],
            "scope_id": profile["scope_id"], "namespace": profile["namespace"],
            "owners": {"current_task": {"scope_id": profile["scope_id"], "namespace": profile["namespace"],
                                         "resource_id": current["resource_id"]}},
            "bindings": sorted(bindings, key=lambda item: (item["resource_id"], item["path"])),
            "source_paths": sorted({owner["path"] for owner in unique.values()}), "excluded_roots": []}


def _catalog_compatible(value, profile):
    try:
        ts.require(isinstance(value, dict) and value.get("profile") == ts.CATALOG, "unsupported", "Unknown read catalog.")
        ts.require(all(value.get(key) == profile[key] for key in ("repository_id", "scope_id", "namespace")),
                   "wrong-scope", "Read catalog selects another project identity.")
        current = profile["owners"]["current_task"]
        ts.require(value.get("owners", {}).get("current_task") == {
            "scope_id": profile["scope_id"], "namespace": profile["namespace"], "resource_id": current["resource_id"]},
            "conflict", "Read catalog selects another current task owner.")
        bindings = value.get("bindings")
        ts.require(isinstance(bindings, list), "invalid", "Read catalog bindings are invalid.")
        for owner in profile["owners"].values():
            matches = [item for item in bindings if isinstance(item, dict)
                       and item.get("resource_id") == owner["resource_id"]]
            ts.require(len(matches) == 1 and matches[0].get("path") == owner["path"]
                       and matches[0].get("kind") == "docs" and matches[0].get("visibility") == "public"
                       and matches[0].get("state") == "active", "conflict",
                       "Existing catalog does not expose the selected native owner: " + owner["path"])
    except (AttributeError, TypeError):
        raise ts.ReadError("invalid", "Existing read catalog is malformed.")


def _ram_config(profile, work):
    owner = profile["owners"]["current_task"]
    return {"profile": ram.CONFIG, "repository_id": profile["repository_id"], "scope_id": profile["scope_id"],
            "namespace": profile["namespace"], "store_root": "RAM", "current_task": owner["path"],
            "work_owners": {work["work_id"]: owner["path"]}}


def _ram_compatible(value, profile, work):
    desired = _ram_config(profile, work)
    ts.require(isinstance(value, dict) and value.get("profile") == ram.CONFIG, "unsupported", "Unknown RAM config.")
    ts.require(all(value.get(key) == desired[key] for key in ("repository_id", "scope_id", "namespace")),
               "wrong-scope", "RAM config selects another project identity.")
    ts.require(value.get("current_task") == desired["current_task"]
               and value.get("work_owners", {}).get(work["work_id"]) == desired["current_task"],
               "conflict", "RAM config does not preserve the selected native work owner.")


def _nav_config(profile):
    selected = profile["nav"]
    return {"profile": nav.CONFIG, "repository_id": profile["repository_id"], "scope_id": profile["scope_id"],
            "namespace": profile["namespace"], **selected}


def _nav_compatible(value, profile):
    ts.require(isinstance(value, dict) and value.get("profile") == nav.CONFIG, "unsupported", "Unknown NAV config.")
    ts.require(all(value.get(key) == profile[key] for key in ("repository_id", "scope_id", "namespace")),
               "wrong-scope", "NAV config selects another project identity.")
    if profile.get("nav") is not None:
        ts.require(all(value.get(key) == expected for key, expected in profile["nav"].items()),
                   "conflict", "Existing NAV configuration differs from the selected profile.")


# Implementation module -> the stable launcher the installer copies into H/bin (entry points in pyproject.toml).
LAUNCHERS = {"cli": "eutonos", "release": "eutonos-version", "dashboard_cli": "eutonos-dashboard",
             "ram_cli": "eutonos-ram", "nav_cli": "eutonos-nav", "setup_cli": "eutonos-setup",
             "workflow_cli": "eutonos-workflow", "host_cli": "eutonos-host", "companion_capture": "eutonos-capture",
             "companion_ram_capture": "eutonos-ram-capture", "companion_control": "eutonos-companion",
             "measurement_cli": "eutonos-measurement", "communication_cli": "eutonos-communication",
             "ram_relevance_cli": "eutonos-ram-relevance", "boards_cli": "eutonos-boards",
             "settings_cli": "eutonos-settings", "assignments_cli": "eutonos-assignments"}


def _wrapper(modules):
    routes = {"docs": ("hub", "cli"), "dashboard": ("dashboard", "dashboard_cli"),
              "ram": ("ram", "ram_cli"), "nav": ("nav", "nav_cli")}
    selected = {"setup": "setup_cli", "workflow": "workflow_cli", "version": "release", "host": "host_cli", "capture": "companion_capture", "ram-capture": "companion_ram_capture", "companion": "companion_control", "measurement": "measurement_cli", "communication": "communication_cli"}
    if "ram" in modules:
        selected["ram-relevance"] = "ram_relevance_cli"
    if "docs" in modules:
        selected["boards"] = "boards_cli"
    if "delegation" in modules:
        selected["settings"] = "settings_cli"
        selected["assignments"] = "assignments_cli"
    for module in modules:
        if module in routes:
            name, implementation = routes[module]
            selected[name] = implementation
    encoded_routes = repr(dict(sorted(selected.items())))
    encoded_launchers = repr({implementation: LAUNCHERS[implementation] for implementation in sorted(set(selected.values()))})
    return ("\"\"\"Managed EUTONOS entry for this adoption profile.\"\"\"\n"
            "import os\nfrom pathlib import Path\nimport subprocess\nimport sys\n\n"
            "modules = " + encoded_routes + "\n"
            "launchers = " + encoded_launchers + "\n"
            "if len(sys.argv) < 2 or sys.argv[1] not in modules:\n"
            "    raise SystemExit(\"Choose: \" + \", \".join(modules))\n"
            "# Parity copy of eutonos.home.home(): EUTONOS_HOME (tests and staging) wins, else ~/.eutonos.\n"
            "home = Path(os.environ.get(\"EUTONOS_HOME\") or Path.home() / \".eutonos\")\n"
            "if not home.is_absolute():\n"
            "    raise SystemExit(\"EUTONOS_HOME must be an absolute path.\")\n"
            "implementation = modules[sys.argv[1]]\n"
            "launcher = home / \"bin\" / (launchers[implementation] + (\".exe\" if os.name == \"nt\" else \"\"))\n"
            "python = os.environ.get(\"EUTONOS_PYTHON\")\n"
            "root = Path(__file__).resolve().parent.parent\n"
            "if python:\n"
            "    if not Path(python).is_file():\n"
            "        raise SystemExit(\"EUTONOS runtime missing; install an exact wheel or set EUTONOS_PYTHON.\")\n"
            "    command = [python, \"-m\", \"eutonos.\" + implementation]\n"
            "elif launcher.is_file():\n"
            "    command = [str(launcher)]\n"
            "else:\n"
            "    raise SystemExit(\"EUTONOS runtime missing; install EUTONOS (its commands live in \" + str(launcher.parent) + \") or set EUTONOS_PYTHON.\")\n"
            "raise SystemExit(subprocess.call([*command, *sys.argv[2:], \"--root\", str(root)]))\n")


def _block(body):
    pattern = re.compile(re.escape(BLOCK_START.encode()) + rb".*?" + re.escape(BLOCK_END.encode()) + rb"(?:\r?\n)?", re.S)
    matches = list(pattern.finditer(body))
    return matches, pattern


def _source_fingerprint():
    return digest(Path(__file__).read_bytes() + CONTRACT.encode("utf-8")
                  + Path(rule_index.__file__).read_bytes() + rule_index.source_bytes())


def _load_manifest(local):
    value = _read_json(local, MANIFEST_PATH)
    if value is None:
        return None
    return _validate_manifest(value)


def _validate_manifest(value):
    ts.shape(value, {"profile", "package_version", "source_fingerprint", "profile_fingerprint",
                     "identity_owners_fingerprint", "adoption_profile", "selected_modules", "managed"})
    ts.require(value["profile"] == MANIFEST and isinstance(value["managed"], dict),
               "unsupported", "Unknown adoption manifest.")
    adopted = validate_profile(value["adoption_profile"])
    identity = {key: adopted[key] for key in ("repository_id", "scope_id", "namespace", "owners")}
    ts.require(adopted == value["adoption_profile"]
               and value["profile_fingerprint"] == digest(ts.encoded(adopted))
               and value["identity_owners_fingerprint"] == digest(ts.encoded(identity))
               and value["selected_modules"] == adopted["modules"],
               "conflict", "Adoption manifest fields do not bind one canonical profile.")
    for key in ("source_fingerprint", "profile_fingerprint", "identity_owners_fingerprint"):
        ts.require(isinstance(value[key], str) and bool(re.fullmatch(r"[0-9a-f]{64}", value[key])),
                   "invalid", "Adoption manifest fingerprint is invalid.")
    ts.string(value["package_version"])
    for path, entry in value["managed"].items():
        safe_path(path)
        ts.shape(entry, {"mode", "sha256"})
        ts.require(entry["mode"] in {"whole", "block"}, "invalid", "Unknown managed output mode.")
        ts.require(isinstance(entry["sha256"], str) and bool(re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])),
                   "invalid", "Managed output fingerprint is invalid.")
    return value


def discover(local):
    """Inspect explicit adoption owners/configurations without crawling source."""
    view = GitView(local.root, "HEAD")
    owners, diagnostics = [], []
    observed = {}
    for path in ("AGENTS.md", "eutonos.read.json", "eutonos.ram.json", "eutonos.nav.json",
                 MANIFEST_PATH, "tools/eutonos_project.py"):
        try:
            observed[path] = local.read(path)
        except ts.ReadError as exc:
            observed[path] = None
            diagnostics.append({"path": path, "code": exc.code})
    present = {path: body is not None for path, body in observed.items()}
    for config_path, key in (("eutonos.read.json", "catalog"), ("eutonos.ram.json", "ram")):
        try:
            value = _read_json(local, config_path)
            if value is not None:
                ts.require(isinstance(value, dict), "invalid", "Adoption configuration must be an object.")
            if key == "catalog" and value:
                refs = value.get("bindings", [])
                ts.require(isinstance(refs, list), "invalid", "Catalog bindings must be an array.")
                owners.extend({"path": item.get("path"), "resource_id": item.get("resource_id"), "source": config_path}
                              for item in refs if isinstance(item, dict) and item.get("kind") == "docs")
            elif key == "ram" and value:
                ts.require(isinstance(value.get("current_task"), str), "invalid", "RAM current task owner is invalid.")
                owners.append({"path": value["current_task"], "resource_id": None, "source": config_path})
        except (OSError, UnicodeError, ValueError, TypeError, ts.ReadError) as exc:
            diagnostics.append({"path": config_path, "code": getattr(exc, "code", "invalid")})
    if not owners:
        for path in ("docs/CURRENT_TASK.md", "WORK.md"):
            if local.read(path) is not None:
                owners.append({"path": path, "resource_id": None, "source": "fallback-candidate"})
    for owner in owners:
        try:
            owner["work"] = _work(local, safe_path(owner["path"]))["work_id"]
            owner["status"] = "selected"
        except (OSError, TypeError, ts.ReadError) as exc:
            owner["status"] = "needs-explicit-adaptation"
            owner["diagnostic"] = getattr(exc, "code", "invalid")
    delegation_managed = len(_block(observed["AGENTS.md"] or b"")[0]) == 1
    wrapper_managed = bool(observed["tools/eutonos_project.py"]
                           and b'Managed EUTONOS entry for this adoption profile.' in observed["tools/eutonos_project.py"])
    configured = (["docs"] if present["eutonos.read.json"] else []) + \
                 (["delegation"] if delegation_managed else []) + \
                 (["dashboard"] if wrapper_managed and b"'dashboard': 'dashboard_cli'" in observed["tools/eutonos_project.py"] else []) + \
                 (["ram"] if present["eutonos.ram.json"] else []) + \
                 (["nav"] if present["eutonos.nav.json"] else [])
    return {"profile": PROFILE, "outcome": "discovered", "root": str(local.root), "commit": view.commit,
            "classification": "managed-adoption" if present[MANIFEST_PATH] else "existing-project",
            "configured_candidates": configured, "owner_candidates": owners, "diagnostics": diagnostics,
            "qualification": "Candidates are observations, not permission, ownership acceptance, or a write plan."}


def plan_profile(local, value):
    profile = validate_profile(value)
    profile_fingerprint = digest(ts.encoded(profile))
    identity_owners_fingerprint = digest(ts.encoded({key: profile[key]
        for key in ("repository_id", "scope_id", "namespace", "owners")}))
    prior = _load_manifest(local)
    conflicts = []
    if prior and prior["identity_owners_fingerprint"] != identity_owners_fingerprint:
        conflicts.append({"path": MANIFEST_PATH, "code": "owner-identity-changed",
                          "message": "Managed upgrades cannot remap project identity or native owners."})
    if prior and not set(prior["selected_modules"]) <= set(profile["modules"]):
        conflicts.append({"path": MANIFEST_PATH, "code": "module-removal",
                          "message": "Managed module removal requires an explicit preservation plan "
                                     "(`eutonos remove-project --de-adopt --plan PLAN.json`)."})
    prior_managed = prior.get("managed", {}) if prior else {}
    for path, entry in prior_managed.items():
        try:
            safe_path(path)
            ts.shape(entry, {"mode", "sha256"})
            ts.require(entry["mode"] in {"whole", "block"}, "invalid", "Unknown managed output mode.")
            current = local.read(path)
            if entry["mode"] == "block":
                matches, _ = _block(current or b"")
                actual = digest(matches[0].group()) if len(matches) == 1 else None
            else:
                actual = digest(current)
            if actual != entry["sha256"]:
                conflicts.append({"path": path, "code": "locally-modified",
                                  "message": "Previously managed output differs from its adoption manifest."})
        except (OSError, TypeError, ts.ReadError):
            conflicts.append({"path": path, "code": "invalid-managed-output",
                              "message": "Managed output metadata or path is invalid."})

    preconditions = set()
    for role, owner in profile["owners"].items():
        preconditions.add(owner["path"])
        try:
            raw = local.read(owner["path"])
            ts.require(raw is not None and _text(raw, owner["path"]).strip(), "missing", "Owner is absent or empty.")
            if role == "current_task" and set(profile["modules"]) & {"docs", "ram"}:
                _work(local, owner["path"])
        except (OSError, TypeError, ts.ReadError) as exc:
            conflicts.append({"path": owner["path"], "code": getattr(exc, "code", "invalid"),
                              "message": getattr(exc, "message", "Owner needs explicit adaptation.")})

    generated = {}
    def managed_json(path, desired, compatible):
        current = local.read(path)
        if path in prior_managed or current is None:
            generated[path] = _json_bytes(desired)
        else:
            try:
                compatible(ts.loads(_text(current, path)))
                preconditions.add(path)
            except (OSError, UnicodeError, ValueError, TypeError, ts.ReadError) as exc:
                conflicts.append({"path": path, "code": getattr(exc, "code", "conflict"),
                                  "message": getattr(exc, "message", "Existing configuration is incompatible.")})

    work = None
    if "ram" in profile["modules"]:
        try:
            work = _work(local, profile["owners"]["current_task"]["path"])
        except ts.ReadError:
            pass  # The owner conflict above remains the single actionable diagnostic.
    if "docs" in profile["modules"]:
        managed_json("eutonos.read.json", _catalog(profile), lambda current: _catalog_compatible(current, profile))
    if "ram" in profile["modules"] and work is not None:
        managed_json("eutonos.ram.json", _ram_config(profile, work), lambda current: _ram_compatible(current, profile, work))
    if "nav" in profile["modules"]:
        existing_nav = local.read("eutonos.nav.json")
        if existing_nav is None and profile.get("nav") is None:
            conflicts.append({"path": "eutonos.nav.json", "code": "needs-explicit-configuration",
                              "message": "Select explicit NAV roots before creating NAV configuration."})
        elif profile.get("nav") is not None:
            managed_json("eutonos.nav.json", _nav_config(profile), lambda current: _nav_compatible(current, profile))
        else:
            try:
                _nav_compatible(ts.loads(_text(existing_nav, "eutonos.nav.json")), profile)
                preconditions.add("eutonos.nav.json")
            except (UnicodeError, ValueError, TypeError, ts.ReadError) as exc:
                conflicts.append({"path": "eutonos.nav.json", "code": getattr(exc, "code", "conflict"),
                                  "message": getattr(exc, "message", "Existing NAV configuration is incompatible.")})

    if "delegation" in profile["modules"]:
        # Additional precedence layers are review leads, never overwritten.
        conflicts.extend(instruction_plan(root=local.root)["conflicts"])
        path = "AGENTS.md"
        current = local.read(path) or b""
        matches, pattern = _block(current)
        if current.count(BLOCK_START.encode()) != len(matches) or current.count(BLOCK_END.encode()) != len(matches):
            conflicts.append({"path": path, "code": "malformed-managed-block", "message": "Delegation markers need explicit reconciliation."})
        elif len(matches) > 1:
            conflicts.append({"path": path, "code": "ambiguous", "message": "Multiple managed delegation blocks exist."})
        elif matches and path not in prior_managed:
            conflicts.append({"path": path, "code": "unknown-managed-block",
                              "message": "A delegation marker exists without the adoption manifest."})
        else:
            desired_block = _delegation_block(profile).encode("utf-8")
            composed = (pattern.sub(lambda match: desired_block, current, count=1) if matches else
                        current + (b"\n\n" if current else b"") + desired_block)
            try:
                generated[path] = merge_block(composed)
            except (ValueError, UnicodeError) as exc:
                conflicts.append({"path": path, "code": str(exc),
                                  "message": "Completion instructions need explicit reconciliation."})
        claude = local.read("CLAUDE.md")
        if claude is None or "CLAUDE.md" in prior_managed:
            generated["CLAUDE.md"] = b"# Repository instructions\n\n@AGENTS.md\n"
        elif re.search(rb"(?m)^@AGENTS\.md\s*$", claude):
            preconditions.add("CLAUDE.md")
        else:
            conflicts.append({"path": "CLAUDE.md", "code": "missing-agents-import",
                              "message": "Preserve existing Claude instructions; explicitly add an AGENTS.md import before adoption."})

    if set(profile["modules"]) & {"docs", "delegation", "dashboard", "ram", "nav"}:
        path = "tools/eutonos_project.py"
        parent = local.root / "tools"
        if not parent.is_dir():
            conflicts.append({"path": path, "code": "missing-parent",
                              "message": "Create the tools directory explicitly before planning the managed wrapper."})
        elif path not in prior_managed and local.read(path) is not None:
            conflicts.append({"path": path, "code": "unknown-existing-output",
                              "message": "Existing wrapper is not adoption-managed; preserve and adapt it explicitly."})
        else:
            generated[path] = _wrapper(profile["modules"]).encode("utf-8")

    if conflicts:
        return {"profile": PROFILE, "outcome": "conflict", "conflicts": conflicts,
                "qualification": "No setup plan was created and no files were written."}
    managed = deepcopy(prior_managed)
    for path, body in generated.items():
        if path == "AGENTS.md":
            matches, _ = _block(body)
            managed[path] = {"mode": "block", "sha256": digest(matches[0].group())}
        else:
            managed[path] = {"mode": "whole", "sha256": digest(body)}
    manifest = {"profile": MANIFEST, "package_version": __version__, "source_fingerprint": _source_fingerprint(),
                "profile_fingerprint": profile_fingerprint, "identity_owners_fingerprint": identity_owners_fingerprint,
                "adoption_profile": profile, "selected_modules": profile["modules"], "managed": managed}
    generated[MANIFEST_PATH] = _json_bytes(manifest)
    recipe = {"profile": "eutonos.tokenslang.setup-recipe.v1", "files": [
        {"path": path, "content": _text(body, path), "before": digest(local.read(path))}
        for path, body in sorted(generated.items())]}
    from .setup_cli import prepare
    result = prepare(local, recipe, inputs=sorted(preconditions - generated.keys()))
    return {**result, "adoption_profile": profile, "adoption_manifest": manifest,
            "qualification": "Preservation-first managed adoption plan; application still requires an existing host and exact selected paths."}


def verify_profile(local, value):
    profile = validate_profile(value)
    manifest = _load_manifest(local)
    ts.require(manifest is not None, "missing", "Adoption manifest is absent; plan and apply the profile first.")
    ts.require(manifest["profile_fingerprint"] == digest(ts.encoded(profile)), "conflict", "Applied profile differs.")
    ts.require(manifest["source_fingerprint"] == _source_fingerprint()
               and manifest["package_version"] == __version__, "stale", "Managed adoption needs an upgrade plan from this runtime.")
    checked = []
    for path, entry in manifest["managed"].items():
        current = local.read(safe_path(path))
        if entry["mode"] == "block":
            matches, _ = _block(current or b"")
            actual = digest(matches[0].group()) if len(matches) == 1 else None
        else:
            actual = digest(current)
        ts.require(actual == entry["sha256"], "stale", "Managed output differs: " + path)
        checked.append(path)
    for role, owner in profile["owners"].items():
        raw = local.read(owner["path"])
        ts.require(raw is not None and _text(raw, owner["path"]).strip(), "missing", "Native owner is absent: " + role)
    if set(profile["modules"]) & {"docs", "ram"}:
        _work(local, profile["owners"]["current_task"]["path"])
    if "docs" in profile["modules"]:
        _catalog_compatible(_read_json(local, "eutonos.read.json"), profile)
    if "ram" in profile["modules"]:
        work = _work(local, profile["owners"]["current_task"]["path"])
        _ram_compatible(_read_json(local, "eutonos.ram.json"), profile, work)
    if "nav" in profile["modules"]:
        _nav_compatible(_read_json(local, "eutonos.nav.json"), profile)
    if "delegation" in profile["modules"]:
        current = local.read("AGENTS.md")
        try:
            ts.require(merge_block(current) == current, "stale", "Managed completion contract needs an upgrade.")
        except (ValueError, UnicodeError) as exc:
            raise ts.ReadError("conflict", "Managed completion contract needs explicit reconciliation.") from exc
    return {"profile": PROFILE, "outcome": "verified", "root": str(local.root),
            "selected_modules": profile["modules"], "managed_paths": sorted(checked),
            "checks": ["native owners", "managed byte fingerprints", *
                       (["read catalog"] if "docs" in profile["modules"] else []),
                       *(["RAM configuration"] if "ram" in profile["modules"] else []),
                       *(["NAV configuration"] if "nav" in profile["modules"] else [])],
            "limits": ["No host authority was created", "No deployment or application acceptance claim"]}


# ----------------------------------------------------------------------------------------------- removal
PRESERVATION = "eutonos.tokenslang.adoption-preservation.v1"
# Managed outputs that carry native owners, RAM, NAV or the Docs catalog: a matching hash is not a reason to discard them.
PROTECTED_CONFIGS = ("eutonos.read.json", "eutonos.ram.json", "eutonos.nav.json")


def _manifest_paths(manifest):
    return sorted(manifest["managed"]) + [MANIFEST_PATH]


def protected_paths(manifest):
    """Paths whose default is `keep`: the profile's native owners and the Docs, RAM and NAV configurations."""
    owners = {owner["path"] for owner in manifest["adoption_profile"]["owners"].values()}
    return sorted((owners | set(PROTECTED_CONFIGS)) & set(_manifest_paths(manifest)))


def preservation_template(local):
    """The default preservation plan for this repository: protected paths `keep`, EUTONOS-managed ones `remove`."""
    manifest = _load_manifest(local)
    ts.require(manifest is not None, "missing", "Adoption manifest is absent; there is nothing to de-adopt.")
    protected = set(protected_paths(manifest))
    return {"profile": PRESERVATION,
            "paths": {path: "keep" if path in protected else "remove" for path in _manifest_paths(manifest)}}


def validate_preservation(plan, manifest):
    """The plan lists every adoption-manifest path exactly once as `keep` or `remove`. A protected path may only be
    removed with `{"action": "remove", "discard_native": true}`. Returns {path: "keep" | "remove"}."""
    ts.shape(plan, {"profile", "paths"})
    ts.require(plan["profile"] == PRESERVATION, "unsupported", "Unsupported preservation plan.")
    paths = plan["paths"]
    ts.require(isinstance(paths, dict), "invalid", "Preservation plan paths must be an object.")
    expected = set(_manifest_paths(manifest))
    ts.require(set(paths) == expected, "conflict",
               "The preservation plan must list every adoption-manifest path and nothing else; "
               "missing: " + ", ".join(sorted(expected - set(paths))) + "; unknown: " + ", ".join(sorted(set(paths) - expected)))
    protected = set(protected_paths(manifest))
    actions = {}
    for path, choice in paths.items():
        action = choice.get("action") if isinstance(choice, dict) else choice
        ts.require(action in ("keep", "remove"), "invalid", "Each path is `keep` or `remove`: " + path)
        if isinstance(choice, dict):
            ts.shape(choice, {"action"}, {"discard_native"})
        if action == "remove" and path in protected:
            ts.require(isinstance(choice, dict) and choice.get("discard_native") is True, "denied",
                       "Removing a native owner, RAM, NAV or catalog configuration needs an explicit "
                       "{\"action\": \"remove\", \"discard_native\": true}: " + path)
        actions[path] = action
    return actions


def _managed_state(local, path, entry):
    """(state, current bytes): state is `matches`, `absent`, or `changed`."""
    current = local.read(path)
    if current is None:
        return "absent", None
    if entry["mode"] == "block":
        matches, _ = _block(current)
        if not matches:
            return "absent", current
        return ("matches" if len(matches) == 1 and digest(matches[0].group()) == entry["sha256"] else "changed"), current
    return ("matches" if digest(current) == entry["sha256"] else "changed"), current


def plan_removal(local, plan):
    """Read-only: what a de-adoption under this preservation plan would do to each manifest path."""
    manifest = _load_manifest(local)
    ts.require(manifest is not None, "missing", "Adoption manifest is absent; there is nothing to de-adopt.")
    actions = validate_preservation(plan, manifest)
    items, unresolved = [], 0
    for path in sorted(manifest["managed"]):
        entry = manifest["managed"][path]
        item = {"path": path, "mode": entry["mode"], "plan": actions[path]}
        try:
            state, _ = _managed_state(local, path, entry)
        except (OSError, ts.ReadError):
            state = "unreadable"
        if actions[path] == "keep":
            item["result"] = "kept-by-plan"
        elif state == "matches":
            item["result"] = "remove" if entry["mode"] == "whole" else "remove-block"
        elif state == "absent":
            item["result"] = "already-absent"
        else:
            item["result"] = "kept-changed" if state == "changed" else "kept-unreadable"
            item["reason"] = "differs from its adoption manifest; reported and kept" if state == "changed" else "could not be read safely"
            unresolved += 1
        items.append(item)
    final = {"path": MANIFEST_PATH, "mode": "whole", "plan": actions[MANIFEST_PATH]}
    if actions[MANIFEST_PATH] == "keep":
        final["result"] = "kept-by-plan"
    elif unresolved:
        final["result"] = "kept-unresolved"
        final["reason"] = f"{unresolved} path(s) marked remove were kept, so the record of them stays"
    else:
        final["result"] = "remove" if local.read(MANIFEST_PATH) is not None else "already-absent"
    items.append(final)
    return {"profile": PRESERVATION, "outcome": "removal-plan", "root": str(local.root), "paths": items,
            "protected_default_keep": protected_paths(manifest),
            "qualification": "Read-only. The manifest hash must still match at apply time; changed files are kept."}


def _without_block(current, match):
    before, after = current[:match.start()], current[match.end():]
    if before.endswith(b"\n\n") and not after.startswith(b"\n"):
        before = before[:-1]  # the blank line adoption added in front of its block
    return before + after


def apply_removal(local, plan, *, backup_dir):
    """Remove the `remove` paths whose current bytes still match the manifest; keep and report the rest. Each removed
    file or block is first copied under `backup_dir`. The manifest goes last, and only when nothing was left over."""
    report = plan_removal(local, plan)  # rechecks the manifest and every hash right before writing
    manifest = _load_manifest(local)
    backup_dir = Path(backup_dir)
    removed, kept = [], []
    for item in report["paths"]:
        path, result = item["path"], item["result"]
        if result in ("remove", "remove-block"):
            if path == MANIFEST_PATH:
                state, current = "matches", local.read(path)
            else:
                state, current = _managed_state(local, path, manifest["managed"][path])
            if state != "matches" or current is None:
                kept.append({"path": path, "reason": "changed while the plan was applied; kept"})
                continue
            target = local.path(path)
            saved = backup_dir / path
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(current)
            if result == "remove-block":
                matches, _ = _block(current)
                updated = _without_block(current, matches[0])
                temporary = target.with_name(target.name + ".eutonos-tmp")
                try:
                    temporary.write_bytes(updated)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
            else:
                target.unlink()
            removed.append({"path": path, "how": "block" if result == "remove-block" else "file", "backup": str(saved)})
        elif result.startswith("kept"):  # kept by the plan, or left because it changed
            kept.append({"path": path, "reason": item.get("reason", "kept by the preservation plan")})
    return {"profile": PRESERVATION, "outcome": "de-adopted" if not any(i["result"] in ("kept-changed", "kept-unreadable", "kept-unresolved") for i in report["paths"])
            else "partial", "removed": removed, "kept": kept, "paths": report["paths"]}


__all__ = ["PROFILE", "MANIFEST", "MANIFEST_PATH", "MODULES", "PRESERVATION", "apply_removal", "discover", "plan_profile",
           "plan_removal", "preservation_template", "profile_description", "validate_preservation", "validate_profile",
           "verify_profile"]
