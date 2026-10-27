"""Select local usage sources and annotate experiments without rebuilding the UI."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone

# Repository-local trial tool, deliberately uses this checkout's implementation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from eutonos.companion_results import CONFIG, project
from eutonos.companion_quality import RUBRIC, validate_experiment, validate_review, review_projection, experiment_fingerprint


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        os.replace(temp, path)
    finally:
        if Path(temp).exists():
            Path(temp).unlink()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    sub = p.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("--detail", action="store_true", help="Explicitly retrieve full per-turn metadata")
    experiment = sub.add_parser("experiment", help="Register task, conditions and an explicit primary/helper pairing")
    experiment.add_argument("--file", required=True, type=Path)
    review = sub.add_parser("review", help="Retain an attributable assessment over pinned completed outputs")
    review.add_argument("--file", required=True, type=Path)
    template = sub.add_parser("review-template", help="Fill observed output IDs and rubric fields without guessing")
    template.add_argument("--experiment", required=True)
    template.add_argument("--output", required=True, type=Path)
    add = sub.add_parser("add-source")
    add.add_argument("--log", required=True, type=Path)
    add.add_argument("--role", choices=("primary", "support", "reviewer"), required=True)
    add.add_argument("--experiment", required=True)
    add.add_argument("--label", required=True)
    add.add_argument("--after")
    add.add_argument("--before")
    annotate = sub.add_parser("annotate")
    annotate.add_argument("--file", required=True, type=Path,
                          help="JSON object keyed by observed thread:turn IDs")
    args = p.parse_args(argv)
    path = args.root / ".eutonos-local" / "companion-results.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "profile": CONFIG, "sources": [], "experiments": [], "annotations": {}}
        if cfg.get("profile") != CONFIG:
            raise ValueError("Invalid results configuration")
        if args.command == "add-source":
            source = {"path": str(args.log.resolve(strict=True)), "role": args.role,
                      "experiment_id": args.experiment, "after": args.after, "before": args.before}
            if source not in cfg["sources"]:
                cfg["sources"].append(source)
            if not any(e["id"] == args.experiment for e in cfg["experiments"]):
                cfg["experiments"].append({"id": args.experiment, "label": args.label,
                                           "kind": "exploratory", "quality": "unreviewed"})
            save(path, cfg)
        elif args.command == "annotate":
            values = json.loads(args.file.read_text(encoding="utf-8"))
            known = {r["id"] for r in project(args.root)["runs"]}
            if not isinstance(values, dict) or not set(values) <= known:
                raise ValueError("Annotations must use observed thread:turn IDs")
            reasons = {"verification", "missing-context", "incorrect-context", "new-scope"}
            for item in values.values():
                if not isinstance(item, dict) or not set(item) <= {"stage", "finding", "quality", "followup_reads"}:
                    raise ValueError("Unknown annotation fields")
                for read in item.get("followup_reads", []):
                    if read.get("reason") not in reasons or not read.get("source"):
                        raise ValueError("Follow-up read requires source route and a supported reason")
                    count = read.get("tokens")
                    if count is not None and (type(count) is not int or count < 0):
                        raise ValueError("Unknown read tokens must be null")
            for key, value in values.items():
                cfg["annotations"].setdefault(key, {}).update(value)
            save(path, cfg)
        elif args.command == "experiment":
            value = validate_experiment(json.loads(args.file.read_text(encoding="utf-8")))
            existing = next((e for e in cfg["experiments"] if e["id"] == value["id"]), None)
            if existing:
                if existing != value:
                    cfg.setdefault("experiment_history", {}).setdefault(value["id"], []).append(dict(existing))
                existing.clear()
                existing.update(value)
            else:
                cfg["experiments"].append(value)
            save(path, cfg)
        elif args.command == "review-template":
            observed = project(args.root)
            if args.experiment not in {e["id"] for e in cfg["experiments"]}:
                raise ValueError("Unknown experiment")
            rows = [r for r in observed["runs"] if r["experiment_id"] == args.experiment and r["role"] == "primary" and r["state"] == "completed" and r.get("final_sha256")]
            if not rows:
                raise ValueError("No completed primary outputs are available to review")
            value = {"experiment_id": args.experiment, "output_summary": "", "reviewer": "",
                     "experiment_fingerprint": experiment_fingerprint(next(e for e in cfg["experiments"] if e["id"] == args.experiment)),
                     "review_kind": "contributor", "retrospective": True,
                     "rubric_id": RUBRIC["id"], "evaluated_at": datetime.now(timezone.utc).isoformat(),
                     "output_refs": [{"run_id": r["id"], "final_sha256": r["final_sha256"]} for r in rows],
                     "dimensions": {d["id"]: {"score": None, "rationale": "", "evidence": []} for d in RUBRIC["dimensions"]},
                     "limitations": []}
            with args.output.open("x", encoding="utf-8") as f:
                json.dump(value, f, indent=2)
                f.write("\n")
            print(json.dumps({"outcome": "template", "output": str(args.output), "note": "Unscored template, not a completed review"}))
            return 0
        elif args.command == "review":
            value = validate_review(json.loads(args.file.read_text(encoding="utf-8")))
            if value["experiment_id"] not in {e["id"] for e in cfg["experiments"]}:
                raise ValueError("Unknown experiment")
            observed = project(args.root)
            assessment = review_projection(value, observed["runs"], next(e for e in cfg["experiments"] if e["id"] == value["experiment_id"]))
            if assessment["status"] != "reviewed":
                raise ValueError("Review does not match completed observed outputs")
            history = cfg.setdefault("reviews", [])
            if not any(r["review_id"] == value["review_id"] for r in history):
                history.append(value)
            save(path, cfg)
        result = project(args.root)
        if not getattr(args, "detail", False):
            result = {"outcome": "ok", "status": result["status"], "run_count": len(result["runs"]),
                      "experiments": [{"id": e["id"], "label": e["label"], "pairing_status": e["pairing_status"],
                                       "quality_score": (e.get("assessment") or {}).get("score")}
                                      for e in result["experiments"]], "diagnostics": result["diagnostics"]}
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"outcome": "error", "message": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
