"""Create one disposable, deterministically committed fixture and run the real CLI."""

import json
import subprocess
import sys

from test_read_core import ReadCoreTests

case = ReadCoreTests()
try:
    case.setUp()
    command = [sys.executable, "-B", "-m", "eutonos", "--root", str(case.root),
               "--revision", case.revision, "orient"]
    before = case.snapshot()
    result = subprocess.run(command, stdout=subprocess.PIPE, check=True)
    case.assertEqual(before, case.snapshot())
    payload = json.loads(result.stdout)
    print(json.dumps(payload, indent=2, ensure_ascii=True))
finally:
    case.doCleanups()
