"""Generate ``src/eutonos/offers/offers.json``, the four lists "What EUTONOS offers" carries inside the app.

    python tools/build_offers.py            # write the file (nothing is touched when it is already right)
    python tools/build_offers.py --check    # exit 1, naming the regenerate command, when the file has drifted
    python tools/build_offers.py --root DIR # read another checkout's catalogue documents

The file is a copy of EUTONOS's own catalogue, so a computer with no project still gets the current and future
features and the current and future capabilities. It is generated, checked in and shipped in the wheel; never edit
it by hand. ``tests/test_offers.py`` fails, and ``tools/build_setup.py`` refuses to build, when it differs from what
this tool produces from the catalogue documents.

Where the records come from: ``docs/REGISTRY.json`` roles ``current-feature-catalogue``, ``future-feature-catalogue``,
``current-capability-catalogue`` and ``future-capability-catalogue`` name the four owner documents (the same way
``eutonos.boards.catalog_owners`` finds them); their ```tokenslang-board``` blocks hold the records.

What is kept:

* only top-level ``feature`` and ``capability`` records (a record with a parent, and every slice, quick, stage and
  campaign, stays out);
* a Current owner contributes records with status ``completed``; a Future owner contributes ``planned`` and
  ``deferred`` records, and ``active`` or ``blocked`` ones, which are shown as status ``in progress``;
* ``cancelled`` and ``superseded`` records stay out wherever they sit;
* every record ships unless its ``extensions.shared`` is ``false`` (an absent value means shared; any other value
  is refused so a typo cannot publish a record that was meant to stay private);
* a capability keeps the IDs in ``extensions.features`` that name a record that also ships, in their written order.

A record in the wrong owner (a completed one in a Future owner, an open one in a Current owner, or a capability in a
feature owner) is refused rather than dropped, because dropping it would silently leave it off a fresh computer's list.

The output is deterministic: sorted, stable JSON, ASCII only, no time stamp, no path or hash of the sources, so the
same catalogue gives the same bytes and Setup.exe stays byte-reproducible (``tools/build_wheel.py``).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
OFFERS_PATH = "src/eutonos/offers/offers.json"
REGENERATE = "python tools/build_offers.py"
PROFILE = "eutonos.offers.v1"
# Section order: current features, future features, current capabilities, future capabilities.
SECTIONS = (("feature", "current"), ("feature", "future"), ("capability", "current"), ("capability", "future"))
CURRENT_STATUS = {"completed": "completed"}
FUTURE_STATUS = {"planned": "planned", "deferred": "deferred", "active": "in progress", "blocked": "in progress"}
LEFT_OUT = {"cancelled", "superseded"}
UNCATEGORIZED = "Uncategorized"


class OffersError(Exception):
    """A refusal with the reason in plain words."""


def _boards():
    sys.path.insert(0, str(ROOT / "src"))
    from eutonos import boards
    from eutonos.effects import LocalRoot
    return boards, LocalRoot


def _shared(item) -> bool:
    value = item.get("extensions", {}).get("shared", True)
    if not isinstance(value, bool):
        raise OffersError(f"{item['id']}: extensions.shared must be true or false, not {value!r}.")
    return value


def _category(item) -> str:
    value = item.get("extensions", {}).get("category")
    return value.strip() if isinstance(value, str) and value.strip() else UNCATEGORIZED


def _offer(item, horizon):
    table = CURRENT_STATUS if horizon == "current" else FUTURE_STATUS
    result = {"id": item["id"], "type": item["type"], "title": item["title"], "summary": item["summary"],
              "category": _category(item), "status": table[item["status"]], "horizon": horizon}
    if item["type"] == "capability":
        result["features"] = list(item.get("extensions", {}).get("features", []))
    return result


def _sort_key(offer):
    order = {section: number for number, section in enumerate(SECTIONS)}
    return (order[(offer["type"], offer["horizon"])], offer["category"].casefold(), offer["category"],
            offer["title"].casefold(), offer["title"], offer["id"])


def collect(root):
    """Return ``(payload, notes)`` for the catalogue documents under ``root``; ``notes`` are plain-words remarks."""
    boards, LocalRoot = _boards()
    local = LocalRoot(root)
    owners = boards.catalog_owners(local)
    found = {(kind, horizon) for horizon, kind in owners.values()}
    absent = [f"{horizon} {kind}" for kind, horizon in SECTIONS if (kind, horizon) not in found]
    if absent:
        raise OffersError("docs/REGISTRY.json names no owner for the " + ", ".join(absent)
                          + " catalogue (roles current-feature-catalogue, future-feature-catalogue, "
                          "current-capability-catalogue, future-capability-catalogue).")
    shipped, notes = {}, []
    for path in sorted(owners):
        horizon, kind = owners[path]
        raw = local.read(path)
        if raw is None:
            raise OffersError(f"The catalogue owner {path} is missing.")
        try:
            board, _ = boards._parse(raw, path)
        except boards.ts.ReadError as exc:
            raise OffersError(f"The catalogue owner {path} cannot be read: {exc}") from exc
        if board is None:
            raise OffersError(f"The catalogue owner {path} has no tokenslang-board block.")
        for item in board["items"]:
            if item["type"] not in boards.CATEGORY_TYPES or "parent_id" in item or item["status"] in LEFT_OUT:
                continue
            table = CURRENT_STATUS if horizon == "current" else FUTURE_STATUS
            if item["type"] != kind:
                raise OffersError(f"{item['id']} is a {item['type']} but sits in the {horizon} {kind} owner {path}; "
                                  "move it to the owner for its type.")
            if item["status"] not in table:
                raise OffersError(f"{item['id']} has status {item['status']} but sits in the {horizon} owner {path}; "
                                  "move it to the owner for its status.")
            if not _shared(item):
                continue
            if item["id"] in shipped:
                raise OffersError(f"Duplicate catalogue ID: {item['id']}.")
            shipped[item["id"]] = _offer(item, horizon)
    for offer in shipped.values():
        if offer["type"] != "capability":
            continue
        kept = [name for name in offer["features"] if name in shipped and shipped[name]["type"] == "feature"]
        for name in offer["features"]:
            if name not in kept:
                notes.append(f"{offer['id']}: link to {name} left out (not a shipped feature).")
        offer["features"] = kept
    payload = {"profile": PROFILE,
               "sources": [{"path": path, "type": owners[path][1], "horizon": owners[path][0]} for path in sorted(owners)],
               "items": sorted(shipped.values(), key=_sort_key)}
    return payload, notes


def render(payload) -> bytes:
    """The exact bytes of the file: stable key order, ASCII, LF line ends, one final newline."""
    return (json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("ascii")


def build(root) -> bytes:
    return render(collect(root)[0])


def target(root) -> Path:
    return Path(root) / OFFERS_PATH


def counts(payload) -> dict:
    plural = {"feature": "features", "capability": "capabilities"}
    return {f"{horizon} {plural[kind]}": sum(1 for item in payload["items"] if (item["type"], item["horizon"]) == (kind, horizon))
            for kind, horizon in SECTIONS}


def _difference(current: bytes, payload) -> str:
    try:
        old = {item["id"]: item for item in json.loads(current.decode("utf-8"))["items"]}
    except (ValueError, KeyError, TypeError, AttributeError):
        return "the file cannot be read as an offers file"
    new = {item["id"]: item for item in payload["items"]}
    parts = []
    for label, names in (("missing from the file", sorted(set(new) - set(old))),
                         ("only in the file", sorted(set(old) - set(new))),
                         ("changed", sorted(name for name in set(old) & set(new) if old[name] != new[name]))):
        if names:
            parts.append(f"{label}: " + ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else ""))
    return "; ".join(parts) or "the file differs in wording or layout only"


def check(root) -> None:
    """Return quietly when the checked-in file is exactly what the catalogue produces; otherwise raise OffersError."""
    payload, _ = collect(root)
    path = target(root)
    if not path.is_file():
        raise OffersError(f"{OFFERS_PATH} is missing. Generate it with: {REGENERATE}")
    current = path.read_bytes()
    if current != render(payload):
        raise OffersError(f"{OFFERS_PATH} is out of date with the catalogue documents ({_difference(current, payload)}). "
                          f"Regenerate it with: {REGENERATE}  then commit the result.")


def write(root):
    """Write the file. Returns ``(changed, payload, notes)``; an already-right file is left untouched."""
    payload, notes = collect(root)
    data, path = render(payload), target(root)
    changed = not path.is_file() or path.read_bytes() != data
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return changed, payload, notes


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="the checkout to read (default: this repository)")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 when the file has drifted")
    args = parser.parse_args(argv)
    try:
        if args.check:
            check(args.root)
            payload, _ = collect(args.root)
            print(f"build_offers: {OFFERS_PATH} is current ({len(payload['items'])} items).")
            return 0
        changed, payload, notes = write(args.root)
    except OffersError as exc:
        print(f"build_offers: {exc}", file=sys.stderr)
        return 1
    for note in notes:
        print(f"build_offers: note: {note}", file=sys.stderr)
    summary = ", ".join(f"{number} {name}" for name, number in counts(payload).items())
    print(f"build_offers: {'wrote' if changed else 'left unchanged'} {OFFERS_PATH} ({len(payload['items'])} items: {summary}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
