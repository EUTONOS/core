"""Compatibility entry for the packaged EUTONOS companion control."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from eutonos import companion_control as _implementation

globals().update({name: value for name, value in vars(_implementation).items()
                  if not name.startswith("__")})


if __name__ == "__main__":
    raise SystemExit(main())
