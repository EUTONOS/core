"""EUTONOS shared read core. TokenSlang is the canonical language name."""

from .hub import Hub
from .source import GitView
from .tokenslang import PROFILE, ReadError

__all__ = ["Hub", "GitView", "PROFILE", "ReadError"]
import json as _json
from pathlib import Path as _Path

__version__ = _json.loads(_Path(__file__).with_name("release.json").read_text(encoding="utf-8"))["version"]
