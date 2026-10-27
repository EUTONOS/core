"""Require explicit product-catalog assessment independently of RAM capture."""
from copy import deepcopy
from . import boards

FIELDS = ("catalog_changes", "catalog_ids")


def assess(values, root):
    raw = values if isinstance(values, dict) else {}
    prior = raw.get("catalog_assessment")
    supplied = {**(prior if isinstance(prior, dict) else {}), **{k: raw[k] for k in FIELDS if k in raw}}
    changes, ids = supplied.get("catalog_changes"), supplied.get("catalog_ids", [])
    data = boards.project(root)
    items = [i for i in data.get("items", []) if i.get("type") in boards.CATEGORY_TYPES]
    by_id = {i["id"]: i for i in items}
    missing, errors = [], []
    if "catalog_changes" not in supplied:
        missing.append("catalog_changes")
    elif not isinstance(changes, list) or any(not isinstance(x, str) or not x.strip() for x in changes):
        errors.append("catalog_changes must list meaningful changes; use [] explicitly for none.")
    if not isinstance(ids, list) or any(not isinstance(x, str) or not x.strip() for x in ids):
        errors.append("catalog_ids must be a list of saved feature/capability IDs.")
    elif changes and not ids:
        missing.append("catalog_ids")
    elif not changes and ids:
        errors.append("Explain catalog_changes when supplying catalog_ids.")
    else:
        for identity in ids:
            item = by_id.get(identity)
            if item is None:
                errors.append("Catalog record not found: " + identity)
            elif not isinstance(item.get("extensions", {}).get("category"), str) or not item["extensions"]["category"].strip():
                errors.append("Catalog record needs a category: " + identity)
    result = {"outcome": "incomplete" if missing or errors else "verified",
              "prompt": "Did this work add, change or retire features or capabilities? List them in catalog_changes, or use [] for None. Save entries and list their catalog_ids.",
              "catalog_changes": deepcopy(changes), "catalog_ids": deepcopy(ids),
              "missing_fields": missing, "errors": errors,
              "categories": [*boards.category_choices(root), "Other"],
              "existing_entries": [{"id": i["id"], "title": i["title"], "type": i["type"],
                                    "category": i.get("extensions", {}).get("category")} for i in items],
              "next": ("Answer catalog_changes with [] when no feature or capability changed; use catalog_ids: [] too. "
                       "If one changed, save its entry through boards form --type feature or capability, "
                       "then provide the saved catalog ID and repeat closeout."
                       if not changes else
                       "Save the changed feature or capability through boards form with its category, "
                       "then provide its saved catalog ID and repeat closeout."),
              "qualification": "Verifies recorded IDs and categories, not completeness or correctness of the agent assessment."}
    if not missing and not errors:
        result.pop("existing_entries", None)
    return result


def strip(values):
    if not isinstance(values, dict):
        return values
    return {k: deepcopy(v) for k, v in values.items() if k not in (*FIELDS, "catalog_assessment")}
