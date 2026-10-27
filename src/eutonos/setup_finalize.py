"""Zero-write, freshly verified finalization plan for `eutonos-setup apply`.

`eutonos-setup finalize --root ROOT` prints the plan as JSON. Review it, then apply
its selected fingerprint through the existing host (`eutonos-setup apply`).
"""
from datetime import datetime, timezone
import json
import platform

from . import __version__, nav_runner, setup_cli, tokenslang as ts
from .effects import LocalRoot, digest, plan

_HISTORICAL_KEYS = ("at", "communication", "dashboard", "measurement", "nav_packets", "ram",
                    "replacement_readers", "source_provider")


def finalize_plan(root_path):
    """Return the finalization setup plan for the project at `root_path`; writes nothing."""
    root = LocalRoot(root_path)
    current = setup_cli.verify(root)
    prior = root.read("eutonos.acceptance.json")
    evidence = json.loads(prior) if prior else {}
    retained = {key: evidence.pop(key) for key in _HISTORICAL_KEYS if key in evidence}
    if retained:
        evidence["prior_acceptance"] = {
            "qualification": "Retained from an earlier acceptance. This finalization does not re-run those checks.",
            **retained,
        }
    evidence.update(current)
    evidence["finalized_at"] = datetime.now(timezone.utc).isoformat()
    evidence["host_observation"] = {"machine": platform.machine(), "python": platform.python_version(),
                                    "system": platform.system()}
    source = nav_runner.Session(root).observe()[0]
    evidence["source_fingerprint"] = source["fingerprint"]
    manifest = json.loads(root.read("eutonos.installation.json"))
    manifest.update(status="locally-verified", package_version=__version__, evidence="eutonos.acceptance.json")
    # Effect inputs are capped at 64 paths. Recheck the source fingerprint when
    # applying the selected plan; these individual owners also stay exact.
    inputs = set(manifest["owners"].values()) | {"eutonos.read.json", "eutonos.nav.json", "eutonos.ram.json",
        "eutonos.communication.json", "eutonos.measurement.json", "NAV/NAVIGATION.md", "NAV/SURFACES.md",
        "NAV/JOURNEYS.md", "NAV/SOURCE_FAMILIES.md", "NAV/NAV_RUN.json"}
    effect = plan(root, {"eutonos.installation.json": ts.encoded(manifest) + b"\n",
                         "eutonos.acceptance.json": ts.encoded(evidence) + b"\n"}, inputs=inputs,
                  nav_source_fingerprint=source["fingerprint"])
    confirmed = setup_cli.verify(root)
    ts.require(confirmed["owners"] == current["owners"] and confirmed["commit"] == current["commit"],
               "stale", "Source changed during finalization.")
    return {"profile": "eutonos.tokenslang.setup-plan.v1", "effect": effect,
            "fingerprint": digest(ts.encoded(effect)), "paths": [c["path"] for c in effect["changes"]],
            "qualification": "Local target verification, not application or independent AI acceptance."}
