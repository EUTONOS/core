"""Deterministic tests for the Simon helper proof of concept."""
import os

# The installed launcher sets SIMON_RUNTIME_DIR; these tests always use the checkout's own .runtime layout.
os.environ.pop("SIMON_RUNTIME_DIR", None)
