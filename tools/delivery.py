"""Source entry for the opt-in ASW Git delivery scaffold."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from eutonos.git_delivery_cli import main
if __name__=='__main__':
    raise SystemExit(main())
