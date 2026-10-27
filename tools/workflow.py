"""Repository convenience entry for the installed-compatible workflow CLI."""
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
from eutonos.workflow_cli import main

if __name__ == "__main__":
    raise SystemExit(main(default_root=str(root)))
