"""Run the Docs CLI -> guarded fixture apply -> committed Hub pickup -> rollback."""
import json
import subprocess
import sys

from eutonos import GitView, Hub, tokenslang as ts
from test_docs_adoption import DocsTests


case = DocsTests()
case.setUp()
try:
    (case.root / "adoption.json").write_bytes(ts.encoded(case.spec))
    before = case.snapshot()
    raw = subprocess.check_output([sys.executable, "-B", "-m", "eutonos.docs_cli", "--root", str(case.root), "--spec", "adoption.json"])
    adoption = json.loads(raw)["adoption"]
    assert before == case.snapshot()
    case.prepare_host(adoption)
    applied = case.apply(adoption)
    revision = case.commit()
    result = Hub(GitView(case.root, revision)).handle({"profile": ts.PROFILE, "request_id": "docs-demo", "operation": "orient",
        "max_bytes": 16384, "purpose": "Pick up the preserved native work after fixture adoption"})
    assert result["outcome"] == "ok"
    rolled_back = case.coordinator.rollback(case.auth, "docs-install", 1, "docs-apply")
    assert rolled_back["outcome"] == "rolled-back"
    print(json.dumps({"language": "TokenSlang", "planning_writes": 0, "apply": applied["outcome"],
                      "selected_work": result["data"]["work"]["work_id"], "coverage": result["coverage"],
                      "rollback": rolled_back["outcome"], "next_module_dispatched": False}))
finally:
    case.doCleanups()
