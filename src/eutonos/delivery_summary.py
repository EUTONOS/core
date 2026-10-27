"""On-demand, read-only delivery state derived from Git and native boards."""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import re
import subprocess
from .hidden_process import no_window

from .private_scope import require_shared
from .boards import project as boards_project


PROFILE = "eutonos.delivery-summary.v1"
_ACTIONS = ("commit", "publish")


def _git(root: Path, *args: str, optional: bool = False) -> str | None:
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_OPTIONAL_LOCKS="0",
               GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(
        ["git", "-c", "safe.directory=" + str(root), "-c", "core.quotepath=false",
         "-C", str(root), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=10, **no_window(),
    )
    if result.returncode == 0:
        return result.stdout.rstrip("\r\n")
    if optional:
        return None
    detail = result.stderr.strip() or "Git read failed."
    raise ValueError(detail)


def _paths(root: Path, *args: str) -> set[str]:
    value = _git(root, *args)
    return {path for path in value.split("\0") if path}


def _authority(value):
    """Normalize caller-supplied authority without treating silence as permission."""
    if value is None:
        return {action: {"authorized": None, "source": None} for action in _ACTIONS}
    if not isinstance(value, dict) or set(value) - set(_ACTIONS):
        raise ValueError("Authority may only describe commit and publish.")
    result = {}
    for action in _ACTIONS:
        supplied = value.get(action)
        if isinstance(supplied, bool) or supplied is None:
            result[action] = {"authorized": supplied, "source": None}
        elif (isinstance(supplied, dict)
              and set(supplied) <= {"authorized", "source"}
              and (isinstance(supplied.get("authorized"), bool)
                   or supplied.get("authorized") is None)
              and (supplied.get("source") is None
                   or isinstance(supplied.get("source"), str))):
            result[action] = {"authorized": supplied.get("authorized"),
                              "source": supplied.get("source")}
        else:
            raise ValueError("Authority values must be true, false, null, or an authorized/source object.")
    return result


def _campaign_index(board):
    items = board.get("items", [])
    by_id = {item["id"]: item for item in items}
    campaigns = {}
    claims = {}
    for item in items:
        current = item
        seen = set()
        while current.get("type") != "campaign" and current.get("parent_id") in by_id:
            if current["id"] in seen:
                current = {}
                break
            seen.add(current["id"])
            current = by_id[current["parent_id"]]
        campaign = current if current.get("type") == "campaign" else None
        if campaign:
            verification = {
                "status": "recorded-complete" if campaign["status"] == "completed" else "not-recorded-complete",
                "evidence": [{"path": campaign["owner_path"], "kind": "campaign-owner"}],
                "qualification": ("Native board status and owner reference only; checks were not rerun by this read."),
            }
            campaigns.setdefault(campaign["id"], {
                "id": campaign["id"], "title": campaign["title"],
                "status": campaign["status"], "owner_path": campaign["owner_path"],
                "verification": verification,
            })
        references = list(item.get("evidence_refs", []))
        if item.get("type") == "campaign":
            references.append({"path": item["owner_path"]})
        for reference in references:
            path = reference.get("path")
            if path and campaign:
                claims.setdefault(path, set()).add(campaign["id"])
                evidence = {"path": path, "kind": "explicit-evidence"}
                if evidence not in campaigns[campaign["id"]]["verification"]["evidence"]:
                    campaigns[campaign["id"]]["verification"]["evidence"].append(evidence)
    return campaigns, claims


def _commit_claims(root, campaigns, upstream):
    """Map unpublished paths to explicit Eutonos-Campaign commit trailers."""
    if not upstream:
        return {}, {}, []
    raw = _git(root, "log", "-z", "--format=%x1e%H%x00%B%x00", "--name-only",
               upstream + "..HEAD", optional=True)
    if raw is None:
        return {}, {}, ["Unpublished commit campaign attribution is unavailable."]
    claims, evidence_by_path, diagnostics = {}, {}, []
    for record in raw.split("\x1e"):
        if not record:
            continue
        fields = record.split("\0")
        if len(fields) < 2:
            diagnostics.append("An unpublished commit record could not be parsed for campaign attribution.")
            continue
        commit, body = fields[0].strip(), fields[1]
        campaign_ids = sorted(set(re.findall(r"(?mi)^Eutonos-Campaign:\s*([A-Za-z0-9._-]+)\s*$", body)))
        paths = [value.strip("\r\n") for value in fields[2:] if value.strip("\r\n")]
        if not campaign_ids:
            continue
        unknown = [campaign_id for campaign_id in campaign_ids if campaign_id not in campaigns]
        if unknown:
            diagnostics.append(f"Commit {commit[:12]} names unknown campaign IDs: {', '.join(unknown)}.")
        for path in paths:
            for campaign_id in campaign_ids:
                if campaign_id in campaigns:
                    claims.setdefault(path, set()).add(campaign_id)
                    evidence = evidence_by_path.setdefault(path, {}).setdefault(campaign_id, [])
                    item = {"path": path, "kind": "commit-trailer", "commit": commit}
                    if item not in evidence:
                        evidence.append(item)
    return claims, evidence_by_path, diagnostics


def _ownership(path, campaigns, board_claims, commit_claims, commit_evidence):
    board_ids = sorted(board_claims.get(path, ()))
    ids = board_ids or sorted(commit_claims.get(path, ()))
    if not ids:
        return {"status": "unknown", "campaigns": [],
                "qualification": "No native board campaign explicitly claims this path."}
    owners = []
    for item_id in ids:
        owner = {key: deepcopy(campaigns[item_id][key])
                 for key in ("id", "title", "status", "owner_path", "verification")}
        for evidence in commit_evidence.get(path, {}).get(item_id, []):
            if evidence not in owner["verification"]["evidence"]:
                owner["verification"]["evidence"].append(deepcopy(evidence))
        owners.append(owner)
    return {"status": "identified" if len(owners) == 1 else "ambiguous",
            "campaigns": owners,
            "qualification": (("Matched an explicit native board owner/evidence path."
                               if board_ids else
                               "Matched an explicit campaign trailer on an unpublished commit.")
                              if len(owners) == 1 else
                              "Several native campaigns explicitly claim this path; no owner was inferred.")}


def _action(name, needed, known, authority, blockers=()):
    authorized = authority[name]["authorized"]
    reasons = list(blockers)
    if not known:
        deliverable = None
        reasons.append("Required repository state is unknown.")
    elif not needed:
        deliverable = False
        reasons.append("No matching delivery is pending.")
    elif reasons:
        deliverable = False
    elif authorized is True:
        deliverable = True
    elif authorized is False:
        deliverable = False
        reasons.append("Current authority explicitly excludes this effect.")
    else:
        deliverable = None
        reasons.append("Current authority for this effect is unknown.")
    return {"authorized": authorized, "authority_source": authority[name]["source"],
            "pending": needed if known else None, "deliverable": deliverable,
            "reasons": reasons,
            "qualification": "Advisory only; this projection performs no Git or external effect."}


def project(root, *, authority=None):
    """Compute repository delivery state without changing Git or native records."""
    require_shared(root)
    selected = Path(root).resolve(strict=True)
    top = Path(_git(selected, "rev-parse", "--show-toplevel")).resolve()
    if top != selected:
        raise ValueError("Delivery root must be the exact Git worktree root.")

    branch = _git(selected, "symbolic-ref", "--quiet", "--short", "HEAD", optional=True)
    head = _git(selected, "rev-parse", "HEAD", optional=True)
    upstream = _git(selected, "rev-parse", "--abbrev-ref", "@{upstream}", optional=True)
    ahead = behind = None
    if upstream and head:
        counts = _git(selected, "rev-list", "--left-right", "--count",
                      "HEAD...@{upstream}", optional=True)
        if counts:
            parts = counts.split()
            if len(parts) == 2 and all(part.isdigit() for part in parts):
                ahead, behind = map(int, parts)

    staged = _paths(selected, "diff", "--cached", "--name-only", "-z")
    modified = _paths(selected, "diff", "--name-only", "-z")
    untracked = _paths(selected, "ls-files", "--others", "--exclude-standard", "-z")
    committed = (_paths(selected, "diff", "--name-only", "-z", "@{upstream}...HEAD")
                 if upstream and ahead is not None else set())
    committed_known = upstream is not None and ahead is not None

    board = boards_project(selected)
    campaigns, claims = _campaign_index(board)
    commit_claims, commit_evidence, commit_diagnostics = _commit_claims(
        selected, campaigns, upstream)
    all_paths = sorted(staged | modified | untracked | committed)
    rows = []
    for path in all_paths:
        states = [name for name, values in (("staged", staged), ("modified", modified),
                                             ("untracked", untracked), ("committed", committed))
                  if path in values]
        rows.append({"path": path, "states": states,
                     "staged": path in staged, "modified": path in modified,
                     "untracked": path in untracked,
                     "committed": (path in committed) if committed_known else None,
                     "disposition": "local" if path in staged | modified | untracked else "committed",
                     "ownership": _ownership(path, campaigns, claims, commit_claims,
                                               commit_evidence)})

    local_pending = bool(staged or modified or untracked)
    publication = ("unknown" if ahead is None else
                   "unpublished" if ahead else "published-to-upstream")
    normalized_authority = _authority(authority)
    ownership_blockers = [row["path"] for row in rows
                          if row["disposition"] == "local"
                          and row["ownership"]["status"] != "identified"]
    committed_ownership_blockers = [row["path"] for row in rows
                                    if row["disposition"] == "committed"
                                    and row["ownership"]["status"] != "identified"]
    actions = {
        "commit": _action("commit", local_pending, True, normalized_authority,
                          (["Local paths have unknown or ambiguous campaign ownership: "
                            + ", ".join(ownership_blockers)] if ownership_blockers else [])),
        "publish": _action("publish", bool(ahead), ahead is not None, normalized_authority,
                           ((["The local branch is behind its upstream and needs reconciliation."]
                             if behind else [])
                            + (["Committed paths have unknown or ambiguous campaign ownership: "
                                + ", ".join(committed_ownership_blockers)]
                               if committed_ownership_blockers else []))),
    }
    diagnostics = [*board.get("diagnostics", []), *commit_diagnostics]
    if board.get("status") not in {"available", "not-configured"}:
        diagnostics.append("Campaign ownership is partial because native boards are unavailable or invalid.")
    return {
        "profile": PROFILE,
        "status": "partial" if diagnostics else "available",
        "repository": {"root": str(selected), "branch": branch, "head": head,
                       "upstream": upstream,
                       "divergence": {"ahead": ahead, "behind": behind,
                                      "known": ahead is not None and behind is not None}},
        "paths": rows,
        "totals": {"paths": len(rows), "staged": len(staged), "modified": len(modified),
                   "untracked": len(untracked),
                   "committed": len(committed) if committed_known else None,
                   "ownership_unknown": sum(row["ownership"]["status"] == "unknown" for row in rows),
                   "ownership_ambiguous": sum(row["ownership"]["status"] == "ambiguous" for row in rows)},
        "campaigns": [campaigns[key] for key in sorted(campaigns)],
        "disposition": {"working_tree": "local-changes" if local_pending else "clean",
                        "committed_changes": (bool(committed) if committed_known else None),
                        "publication": publication},
        "authority": actions,
        "diagnostics": diagnostics,
        "qualification": ("Computed from the current worktree, Git refs, and native board records. "
                          "It does not prove review, acceptance, remote readback, installation, or deployment."),
    }


read = project

__all__ = ["PROFILE", "project", "read"]
