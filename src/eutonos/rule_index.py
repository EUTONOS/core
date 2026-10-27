"""Versioned rule references; no policy evaluation, source reads or permissions.

All rows are exposed, including conditional and optional rows. The receiving
agent resolves task applicability against the actual native owners. HAO2's
orientation command and selection/continuation consumer are not implemented here.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

PROFILE = "eutonos.rule-reference-index.v1"
TIERS = {"Required", "Required-if", "Optional"}
RESOURCE = Path(__file__).with_name("rule_index.json")


def source_bytes():
    return RESOURCE.read_bytes()


def validate(value):
    """Validate references only; this does not establish owner/source readiness."""
    if not isinstance(value, dict) or set(value) != {"profile", "rules", "precedence"} or value["profile"] != PROFILE:
        raise ValueError("unsupported-rule-index")
    if not isinstance(value["rules"], list) or not 1 <= len(value["rules"]) <= 32:
        raise ValueError("invalid-rule-index-size")
    ids = set()
    for row in value["rules"]:
        if not isinstance(row, dict) or set(row) != {"id", "owner", "reason", "tier", "applies_when"}:
            raise ValueError("invalid-rule-reference")
        if not isinstance(row["id"], str) or not row["id"].startswith("eutonos.rule.") or row["id"] in ids:
            raise ValueError("duplicate-or-invalid-rule-id")
        ids.add(row["id"])
        if row["tier"] not in TIERS or any(not isinstance(row[key], str) or not row[key].strip()
                                         for key in ("reason", "applies_when")):
            raise ValueError("invalid-rule-reading-need")
        owner = row["owner"]
        if not isinstance(owner, dict):
            raise ValueError("invalid-rule-owner")
        if owner.get("scope") == "native":
            if set(owner) != {"scope", "role"} or not isinstance(owner["role"], str) or not owner["role"]:
                raise ValueError("invalid-native-rule-owner")
        elif owner.get("scope") == "upstream-reference":
            if set(owner) != {"scope", "resource_id", "path", "selection"} or any(
                    not isinstance(owner[key], str) or not owner[key] for key in ("resource_id", "path", "selection")):
                raise ValueError("invalid-upstream-rule-owner")
            path = PurePosixPath(owner["path"])
            if path.is_absolute() or ".." in path.parts or "\\" in owner["path"] or ":" in owner["path"]:
                raise ValueError("invalid-upstream-rule-path")
        else:
            raise ValueError("unknown-rule-owner-scope")
    if not isinstance(value["precedence"], list):
        raise ValueError("invalid-rule-precedence")
    for relation in value["precedence"]:
        if not isinstance(relation, dict) or set(relation) != {"rule", "related", "reason"} or relation["rule"] not in ids:
            raise ValueError("invalid-rule-precedence")
        if (not isinstance(relation["related"], list) or not relation["related"]
                or any(item not in ids for item in relation["related"])
                or not isinstance(relation["reason"], str) or not relation["reason"]):
            raise ValueError("invalid-rule-precedence")
    return value


def load():
    return validate(json.loads(source_bytes()))


def render(owners):
    """Readable references inside the existing managed adoption block.

    Upstream paths are locators within the portable reference tree, not links to
    assumed consumer files. Native owner paths/IDs come only from the profile.
    """
    index = load()
    lines = ["## EUTONOS rule references", "", "Index: `" + PROFILE + "`.",
             "Canonical documents and applicable repository instructions retain authority.",
             "Upstream references are guidance, not consumer owners or installed capabilities.",
             "Keep the readable AGENTS/selected onboarding route if optional orientation is unavailable.", ""]
    for row in index["rules"]:
        owner = row["owner"]
        if owner["scope"] == "native":
            if owner["role"] == "instructions":
                target = "native `AGENTS.md` and applicable harness instructions"
            else:
                bound = owners.get(owner["role"])
                target = ("native `" + bound["path"] + "` (`" + bound["resource_id"] + "`)" if bound
                          else "native owner role `" + owner["role"] + "` (unbound; resolve before dependent work)")
        else:
            target = ("upstream reference `" + owner["path"] + "` :: " + owner["selection"]
                      + " (`" + owner["resource_id"] + "`)")
        lines.append("- `" + row["id"] + "` | " + row["tier"] + " | " + row["applies_when"]
                     + " | " + target + ". Reason: " + row["reason"])
    for relation in index["precedence"]:
        lines.append("- Precedence reference `" + relation["rule"] + "` with "
                     + ", ".join("`" + item + "`" for item in relation["related"]) + ": " + relation["reason"])
    return "\n".join(lines) + "\n"
