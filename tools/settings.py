"""Compatibility entry for the packaged EUTONOS operation."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from eutonos import settings_cli as _implementation

globals().update({name: value for name, value in vars(_implementation).items()
                  if not name.startswith("__")})

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
