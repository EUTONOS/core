"""EHF3 minimal Docs adoption: explicit native roles, zero-write plans, fixtures.

This does not discover the meaning of arbitrary Markdown or migrate the complete
ToVA installer. Existing native content remains its authoritative representation.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import posixpath
import re
from urllib.parse import quote

from . import __version__, tokenslang as ts
from .effects import LocalRoot, digest, plan, validate_plan
from .source import safe_path

PROFILE = "eutonos.tokenslang.docs-adoption.v1"
MANIFEST = "eutonos.tokenslang.docs-installation.v1"
SETUP = "eutonos.tokenslang.setup.v1"
ROLES = {"purpose", "architecture", "current_task", "health"}
BEGIN = "<!-- EUTONOS setup: begin -->"
END = "<!-- EUTONOS setup: end -->"
# Setup status modules, in docs/SETUP.md order. Each selected module carries one state under its own key.
MODULE_ORDER = ("docs", "nav", "ram")
MODULE_LABELS = {"docs": "Docs", "nav": "NAV", "ram": "RAM"}
MODULE_STATES = ("not-started", "configured-unverified", "ready", "paused", "declined")
_FINISHED = ("ready", "declined")
_WAITING = ("not-started", "paused")
_KEEP = object()


def json_bytes(value):
    return ts.encoded(value) + b"\n"


def implementation_digest():
    """Pin actual installed implementation bytes, independent of a moving Git ref."""
    root = Path(__file__).parent
    value = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob("*.py"))}
    return digest(ts.encoded(value))


def utf8(body):
    try:
        return body.decode("utf-8")
    except UnicodeError as exc:
        raise ts.ReadError("unsupported", "Native owner is not UTF-8 text.") from exc


def validate_setup(value):
    """Validate one Setup status record. Docs-only records written before NAV and RAM were tracked still pass."""
    ts.shape(value, {"profile", "selected_modules", "docs", "manifest", "next_module", "preferences"}, {"nav", "ram"})
    ts.require(value["profile"] == SETUP, "unsupported", "Unknown setup profile.")
    selected = ts.sequence(value["selected_modules"])
    ts.require(bool(selected) and all(isinstance(name, str) for name in selected)
               and selected == [name for name in MODULE_ORDER if name in selected] and "docs" in selected,
               "unsupported", "Selected modules are Docs and optionally NAV and RAM, in that order.")
    for name in MODULE_ORDER:
        if name in selected:
            ts.require(value[name] in MODULE_STATES, "unsupported", "Unknown module setup state.")
        else:
            ts.require(name not in value, "invalid", "An unselected module carries no setup state.")
    following = value["next_module"]
    ts.require(following is None or following in MODULE_ORDER, "unsupported", "Unknown next module.")
    ts.require(following is None or following not in selected or value[following] not in _FINISHED,
               "invalid", "The next module is already finished.")
    safe_path(value["manifest"])
    preferences = value["preferences"]
    ts.shape(preferences, {"continuation", "reminders", "last_offer_at", "next_eligible_at", "reoffer_used"})
    ts.string(preferences["continuation"]); ts.string(preferences["reminders"])
    ts.require(preferences["continuation"] in {"paused", "continue-requested"}
               and preferences["reminders"] in {"off", "sleep", "eligible"}
               and type(preferences["reoffer_used"]) is bool, "invalid", "Unknown setup preference.")
    for key in ("last_offer_at", "next_eligible_at"):
        if preferences[key] is not None:
            ts.string(preferences[key])
    return value


def next_module(value):
    """The first selected module still waiting (not started or paused), else the first module not yet selected, else None.
    A configured-but-unverified module is not waiting: its check is a separate step."""
    for name in MODULE_ORDER:
        if name in value["selected_modules"] and value[name] in _WAITING:
            return name
    return next((name for name in MODULE_ORDER if name not in value["selected_modules"]), None)


def progress(value):
    """Counts and one honest sentence, for example "2 of 3 selected modules ready; RAM paused"."""
    selected = value["selected_modules"]
    states = {name: value[name] for name in selected}
    ready = [name for name in selected if states[name] == "ready"]
    notes = [MODULE_LABELS[name] + " " + states[name].replace("-", " ")
             for name in selected if states[name] not in ("ready",)]
    unselected = [MODULE_LABELS[name] for name in MODULE_ORDER if name not in selected]
    text = f"{len(ready)} of {len(selected)} selected modules ready"
    if notes:
        text += "; " + ", ".join(notes)
    if unselected:
        text += "; not selected: " + ", ".join(unselected)
    return {"selected": len(selected), "ready": len(ready), "states": states, "unselected": unselected, "text": text}


def setup_record(body: bytes):
    text = utf8(body)
    blocks = re.findall(r"^```tokenslang-setup\r?\n(.*?)^```[ \t]*\r?$", text, re.M | re.S)
    ts.require(text.count(BEGIN) == text.count(END) == len(blocks) == 1,
               "ambiguous", "Expected one managed setup region and one setup record.")
    start, finish = text.index(BEGIN), text.index(END) + len(END)
    ts.require(start < finish and "```tokenslang-setup" in text[start:finish],
               "invalid", "Setup record is outside its managed region.")
    value = validate_setup(ts.loads(blocks[0]))
    return value, start, finish


def _render(value, catalog_path, routes):
    selected = value["selected_modules"]
    lines = "\n".join("- " + MODULE_LABELS[name] + ": " + (value[name] if name in selected else "not selected")
                      for name in MODULE_ORDER)
    following = value["next_module"]
    return (BEGIN + "\n## EUTONOS setup status\n\n"
            "Docs configuration is readable through `" + catalog_path + "`. Native owners remain authoritative. "
            "Verify the installation before claiming readiness; interrupted apply may be incomplete. "
            "Setup preferences do not dispatch work or schedule reminders.\n\n" + lines + "\n\n"
            + progress(value)["text"] + ". Next module: " + (MODULE_LABELS[following] if following else "none") + ".\n\n"
            + routes + "\n\n"
            "```tokenslang-setup\n" + ts.encoded(value).decode() + "\n```\n" + END)


def render_setup(value, catalog_path, setup_path, owners):
    validate_setup(value)
    grouped = {}
    for role, owner in sorted(owners.items()):
        grouped.setdefault(owner["path"], []).append(role.replace("_", " "))
    routes = "\n".join("- [" + ", ".join(roles) + "](" +
                       quote(posixpath.relpath(path, posixpath.dirname(setup_path) or "."), safe="/.-") + ")"
                       for path, roles in grouped.items())
    return _render(value, catalog_path, routes)


def update_setup(body: bytes, *, selected=None, states=None, following=_KEEP, preferences=None):
    """The agent-entry owner's bytes with the Setup status block updated: module selection, module states, the next
    module (recomputed when not given) and preferences. Everything outside the managed region, and its route
    links and catalog path, are kept byte for byte. Nothing is written; the caller applies the result."""
    value, start, finish = setup_record(body)
    text = utf8(body)
    region = text[start:finish]
    crlf = "\r\n" in region
    flat = region.replace("\r\n", "\n")
    catalog = re.search(r"readable through `([^`]+)`", flat)
    ts.require(catalog is not None, "invalid", "The managed setup region does not name its catalog.")
    routes = "\n".join(re.findall(r"^- \[[^\n]*\]\([^\n]*\)$", flat, re.M))
    changed = ts.loads(ts.encoded(value).decode())
    if selected is not None:
        changed["selected_modules"] = [name for name in MODULE_ORDER if name in selected]
        for name in MODULE_ORDER:
            if name in changed["selected_modules"]:
                changed.setdefault(name, "not-started")
            elif name != "docs":
                changed.pop(name, None)
    for name, state in (states or {}).items():
        ts.require(name in changed["selected_modules"], "invalid", "Select a module before setting its state: " + str(name))
        changed[name] = state
    for key, item in (preferences or {}).items():
        ts.require(key in changed["preferences"], "invalid", "Unknown setup preference: " + str(key))
        changed["preferences"][key] = item
    changed["next_module"] = next_module(changed) if following is _KEEP else following
    validate_setup(changed)
    fresh = _render(changed, catalog.group(1), routes)
    if crlf:
        fresh = fresh.replace("\n", "\r\n")
    return (text[:start] + fresh + text[finish:]).encode("utf-8")


def seed_document(seed):
    ts.shape(seed, {"path", "resource_id", "purpose", "rules", "work"})
    for key in ("purpose", "rules", "resource_id"):
        ts.string(seed[key])
    ts.require(not any(c in seed["resource_id"] for c in "`\r\n"), "unsupported", "Seed ID cannot be represented in the native declaration.")
    safe_path(seed["path"])
    ts.shape(seed["work"], {"profile", "work_id", "goal", "status", "required", "optional"}, {"extensions"})
    ts.require(seed["work"]["profile"] == ts.WORK and seed["work"]["status"] == "selected",
               "unsupported", "Blank adoption needs explicit selected native work.")
    ts.string(seed["work"]["work_id"]); ts.string(seed["work"]["goal"])
    ts.require(bool(ts.sequence(seed["work"]["required"])), "invalid", "Declare the governing source for blank work.")
    for reference in seed["work"]["required"]:
        ts.reference(reference)
    ts.require(seed["work"]["optional"] == [], "unsupported", "Blank seed does not install optional providers.")
    return ("# Project overview\n\nDocument ID: `" + seed["resource_id"] + "`.\n\n"
            "## Purpose\n\n" + seed["purpose"] + "\n\n"
            "## Known state and architecture\n\n"
            "Setup has not verified existing capabilities or established an architecture. "
            "The selected work below records the known plan; further features and lessons belong here when established.\n\n"
            "## Working rules and verification\n\n" + seed["rules"] + "\n\n"
            "## Current work\n\n```tokenslang-work\n" + ts.encoded(seed["work"]).decode() + "\n```\n").encode()


def plan_adoption(root: LocalRoot, spec: dict):
    """The caller supplies reviewed role mappings. No filesystem writes or models."""
    spec = ts.loads(ts.encoded(spec).decode())
    ts.shape(spec, {"profile", "repository_id", "scope_id", "namespace", "owners",
                    "setup_owner", "catalog_path", "manifest_path"}, {"seed"})
    ts.require(spec["profile"] == PROFILE, "unsupported", "Unsupported Docs adoption profile.")
    for field in ("repository_id", "scope_id", "namespace"):
        ts.string(spec[field])
    ts.shape(spec["owners"], ROLES)
    paths = [safe_path(spec[k]) for k in ("setup_owner", "catalog_path", "manifest_path")]
    setup_path, catalog_path, manifest_path = paths
    ts.require(len(set(paths)) == 3, "ambiguous", "Setup/catalog/manifest owners must be distinct.")
    observed, updates = set(paths), {}
    snapshots = {}
    def read(name):
        body = root.read(name)
        ts.require(name not in snapshots or snapshots[name] == body, "stale", "Source changed during planning.")
        snapshots[name] = body
        return body
    seed = spec.get("seed")
    if seed:
        body = seed_document(seed)
        existing = read(seed["path"])
        if existing is None:
            updates[seed["path"]] = body
    by_id, by_path, roles = {}, {}, {}
    def ref(resource_id):
        return {"scope_id": spec["scope_id"], "namespace": spec["namespace"], "resource_id": resource_id}
    for role, owner in sorted(spec["owners"].items()):
        ts.shape(owner, {"path", "resource_id"})
        name, resource_id = safe_path(owner["path"]), ts.string(owner["resource_id"])
        ts.require(name not in {catalog_path, manifest_path}, "conflict", "Technical metadata cannot own native project truth.")
        observed.add(name)
        body = updates.get(name, read(name))
        ts.require(body is not None and bool(body.strip()), "missing", "Native owner needs useful content: " + name)
        declarations = re.findall(r"^Document ID:\s*`([^`]+)`", utf8(body), re.M)
        ts.require(not declarations or declarations == [resource_id], "conflict", "Preserve the native document ID.")
        ts.require(resource_id not in by_id or by_id[resource_id] == name, "ambiguous", "ID has competing owners.")
        ts.require(name not in by_path or by_path[name] == resource_id, "ambiguous", "One native owner has competing new IDs.")
        by_id[resource_id], by_path[name], roles[role] = name, resource_id, ref(resource_id)
    if seed:
        ts.require(by_id.get(seed["resource_id"]) == seed["path"], "conflict", "Seed must be a selected native owner.")
    original_setup = updates.get(setup_path, read(setup_path)) or b""
    if BEGIN in utf8(original_setup) or "```tokenslang-setup" in utf8(original_setup):
        setup, _, _ = setup_record(original_setup)
        ts.require(setup["manifest"] == manifest_path, "conflict", "Existing setup points to another installation owner.")
    else:
        ts.require(not re.search(r"^#{1,6}\s+.*setup\s+status", utf8(original_setup), re.M | re.I),
                   "unsupported", "Map the existing setup owner; do not add a competing checklist.")
        setup = {"profile": SETUP, "selected_modules": ["docs"], "docs": "configured-unverified",
                 "manifest": manifest_path, "next_module": "nav", "preferences": {
                     "continuation": "paused", "reminders": "off", "last_offer_at": None,
                     "next_eligible_at": None, "reoffer_used": False}}
        updates[setup_path] = original_setup + (b"\n\n" if original_setup else b"") + render_setup(setup, catalog_path, setup_path, spec["owners"]).encode() + b"\n"
    # Known older installer evidence is observed and retained, never rewritten.
    legacy = []
    for folder in (".project", ".tova"):
        if (root.root / folder).exists():
            for filename in ("TOVA_INSTALLATION.json", "EXECUTION_STATE.json"):
                name = folder + "/" + filename
                body = read(name)
                observed.add(name)
                if body is not None:
                    legacy.append(name)
    existing_catalog = read(catalog_path)
    catalog = ts.loads(utf8(existing_catalog)) if existing_catalog else {
        "profile": ts.CATALOG, "repository_id": spec["repository_id"], "scope_id": spec["scope_id"],
        "namespace": spec["namespace"], "owners": {"current_task": roles["current_task"]},
        "bindings": [], "excluded_roots": list(root.excluded), "source_paths": []}
    ts.shape(catalog, {"profile", "repository_id", "scope_id", "namespace", "owners", "bindings", "excluded_roots", "source_paths"}, {"extensions"})
    ts.require(catalog["profile"] == ts.CATALOG and all(catalog[k] == spec[k] for k in ("repository_id", "scope_id", "namespace")),
               "conflict", "Existing catalog uses a different scope/profile.")
    ts.require(catalog["owners"] == {"current_task": roles["current_task"]}, "conflict", "Existing native work ownership differs.")
    ts.require(set(ts.sequence(catalog["excluded_roots"])) == set(root.excluded), "conflict", "Preserve the existing nested exclusions.")
    ts.sequence(catalog["bindings"]); ts.sequence(catalog["source_paths"])
    for binding in catalog["bindings"]:
        ts.shape(binding, {"resource_id", "path", "kind", "label", "visibility", "state"},
                 {"aliases", "sha256", "start_line", "end_line", "extensions"})
        for key in ("resource_id", "kind", "label", "visibility", "state"):
            ts.string(binding[key])
        safe_path(binding["path"])
        ts.require(binding["kind"] in {"docs", "source", "nav", "ram"}, "unsupported", "Unknown existing catalog provider.")
        for alias in ts.sequence(binding.get("aliases", [])):
            ts.string(alias)
        ts.reference({k: binding[k] for k in ("path", "start_line", "end_line") if k in binding})
    for name in catalog["source_paths"]:
        safe_path(name)
    for resource_id, name in by_id.items():
        found = [b for b in catalog["bindings"] if b.get("resource_id") == resource_id or resource_id in b.get("aliases", []) or b.get("path") == name]
        ts.require(len(found) <= 1, "ambiguous", "Competing catalog owner bindings.")
        if found:
            b = found[0]
            ts.require(b["resource_id"] == resource_id and b["path"] == name and b["kind"] == "docs"
                       and b["visibility"] == "public" and b["state"] == "active"
                       and "start_line" not in b and "end_line" not in b,
                       "conflict", "Existing owner binding cannot be silently changed.")
            if "sha256" in b:
                ts.require(b["sha256"] == digest(read(name)), "stale", "Existing catalog fingerprint is stale.")
                b["sha256"] = digest(updates.get(name, read(name)))
        else:
            catalog["bindings"].append({"resource_id": resource_id, "path": name, "kind": "docs",
                                        "label": resource_id, "visibility": "public", "state": "active"})
        if name not in catalog["source_paths"]:
            catalog["source_paths"].append(name)
    ts.sequence(catalog["bindings"]); ts.sequence(catalog["source_paths"])
    new_catalog = json_bytes(catalog)
    # Preserve byte formatting on repeat, including opaque optional extensions.
    if existing_catalog is None or ts.loads(utf8(existing_catalog)) != catalog:
        updates[catalog_path] = new_catalog
    manifest = {"profile": MANIFEST, "module": "docs", "package_version": __version__,
                "implementation_sha256": implementation_digest(), "catalog": catalog_path, "roles": roles,
                "setup_owner": setup_path, "legacy_evidence": legacy, "execution": "fixture-only"}
    existing_manifest = read(manifest_path)
    if existing_manifest:
        ts.require(ts.loads(utf8(existing_manifest)) == manifest, "conflict", "Existing installation differs; explicit upgrade mapping required.")
    else:
        updates[manifest_path] = json_bytes(manifest)
    effect = plan(root, updates, inputs=observed)
    # Detect source drift during planning without writing anything.
    validate_plan(root, effect)
    ts.require(all(effect["observed"].get(name) == digest(body) for name, body in snapshots.items()),
               "stale", "Source changed while the plan was assembled.")
    work_body = updates.get(by_id[roles["current_task"]["resource_id"]], read(by_id[roles["current_task"]["resource_id"]]))
    return {"profile": PROFILE, "classification": "legacy-evidence-preserved" if legacy else "blank-explicit-seed" if seed else "native-owners",
            "effect": effect, "fingerprint": digest(ts.encoded(effect)),
            "preservation": "existing-native-bytes-preserved; appended-setup-only",
            "orientation": "tokenslang-work-adapter" if b"```tokenslang-work" in work_body else "native-source-only",
            "next_module": "nav", "next_module_dispatched": False}


def apply_adoption(coordinator, authority, attempt, generation, effect_id, adoption, expected_fingerprint):
    ts.shape(adoption, {"profile", "classification", "effect", "fingerprint", "preservation",
                        "orientation", "next_module", "next_module_dispatched"})
    ts.require(adoption["profile"] == PROFILE
               and digest(ts.encoded(adoption["effect"])) == adoption["fingerprint"] == expected_fingerprint,
               "conflict", "Adoption plan fingerprint differs.")
    # No automatic store initialization, reservation, permission or follow-on module.
    return coordinator.apply(authority, attempt, generation, effect_id, adoption["effect"])
