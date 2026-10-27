"""Check auto-compaction settings for Claude Code and Codex harnesses.

Reports autoCompactWindow from Claude Code settings, model_auto_compact_token_limit
from Codex config.
With --context-tokens, provides recommendations for Claude settings.

Examples:
  python tools/compaction_check.py
  python tools/compaction_check.py --context-tokens 1000000
  python tools/compaction_check.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib

try:
    import tomllib
except ModuleNotFoundError:
    # Python 3.10 compatibility fallback (tomli backport)
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        tomllib = None  # type: ignore


def load_json_safe(path: str) -> dict | None:
    """Load JSON file; return None if missing, raise if malformed."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON parse error in {path}: {e}")


def load_toml_safe(path: str) -> dict | None:
    """Load TOML file; return None if missing, raise if malformed."""
    if not os.path.exists(path):
        return None
    if tomllib is None:
        raise ValueError(f"tomllib not available; cannot parse {path}")
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        raise ValueError(f"TOML parse error in {path}: {e}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--home", default=None, help="override home directory (for tests)")
    ap.add_argument("--root", default=None, help="override project root (default: parent of tools/)")
    ap.add_argument("--context-tokens", type=int, default=None, help="model context window size")
    ap.add_argument("--json", action="store_true", help="output as JSON")
    args = ap.parse_args(argv)

    home = args.home or os.path.expanduser("~")
    if args.root is None:
        # Guess root as the parent of the tools/ directory (where this script is)
        script_dir = os.path.dirname(os.path.abspath(__file__))
        args.root = os.path.dirname(script_dir)

    result = {
        "claude": None,
        "codex": None,
        "recommendation": None,
        "flags": [],
        "errors": [],
    }

    # Claude Code settings
    claude_window = None
    claude_source = None
    try:
        # Start with user settings
        user_claude_path = os.path.join(home, ".claude", "settings.json")
        user_settings = load_json_safe(user_claude_path)
        if user_settings and "autoCompactWindow" in user_settings:
            claude_window = user_settings["autoCompactWindow"]
            claude_source = f"user settings ({user_claude_path})"

        # Project settings override user settings
        project_claude_path = os.path.join(args.root, ".claude", "settings.json")
        project_settings = load_json_safe(project_claude_path)
        if project_settings and "autoCompactWindow" in project_settings:
            claude_window = project_settings["autoCompactWindow"]
            claude_source = f"project settings ({project_claude_path})"

        # Project local settings override all
        project_local_path = os.path.join(args.root, ".claude", "settings.local.json")
        local_settings = load_json_safe(project_local_path)
        if local_settings and "autoCompactWindow" in local_settings:
            claude_window = local_settings["autoCompactWindow"]
            claude_source = f"project local settings ({project_local_path})"

        if claude_window is not None:
            trigger_estimate = claude_window - 33000
            result["claude"] = {
                "autoCompactWindow": claude_window,
                "source": claude_source,
                "trigger_estimate": trigger_estimate,
                "trigger_note": "estimated at window - 33000 tokens",
            }
    except ValueError as e:
        result["errors"].append(str(e))

    # Codex settings
    codex_model = None
    codex_limit = None
    try:
        codex_toml_path = os.path.join(home, ".codex", "config.toml")
        codex_settings = load_toml_safe(codex_toml_path)
        if codex_settings:
            codex_model = codex_settings.get("model")
            codex_limit = codex_settings.get("model_auto_compact_token_limit")
        if codex_limit is not None or codex_model is not None:
            result["codex"] = {
                "model": codex_model if codex_model else "not set",
                "model_auto_compact_token_limit": codex_limit if codex_limit else "not set",
                "source": codex_toml_path,
            }
    except ValueError as e:
        result["errors"].append(str(e))

    # Recommendations
    if args.context_tokens is not None:
        ctx = args.context_tokens
        # Recommend window to trigger at ~88% of context
        recommended_window = round(0.88 * ctx) + 33000
        # Cap to not exceed context window
        recommended_window = min(recommended_window, ctx)

        rec = {
            "context_window": ctx,
            "recommended_autoCompactWindow": recommended_window,
            "rationale": "triggers at ~88% of context window (recommended sweet spot)",
        }
        result["recommendation"] = rec

        # Check current Claude setting against the context window
        if claude_window is not None:
            trigger_percent = ((claude_window - 33000) / ctx) * 100 if ctx > 0 else 0
            if trigger_percent > 95:
                result["flags"].append(
                    f"Claude setting triggers at {trigger_percent:.1f}% of context (above 95% "
                    f"risk threshold); may hit hard limit"
                )
            elif trigger_percent < 60:
                result["flags"].append(
                    f"Claude setting triggers at {trigger_percent:.1f}% of context (below 60% "
                    f"threshold); compacting very early"
                )

    # Output
    if args.json:
        # Clean up None values for JSON output
        def clean_none(obj):
            if isinstance(obj, dict):
                return {k: clean_none(v) for k, v in obj.items() if v is not None}
            elif isinstance(obj, list):
                return [clean_none(item) for item in obj if item is not None]
            return obj

        result = clean_none(result)
        print(json.dumps(result, indent=2))
    else:
        # Human-readable output
        print("=== Claude Code ===")
        if result["claude"]:
            print(f"  autoCompactWindow: {result['claude']['autoCompactWindow']}")
            print(f"  Source: {result['claude']['source']}")
            print(f"  Trigger estimate: {result['claude']['trigger_estimate']} tokens")
        else:
            print("  autoCompactWindow: not set")

        print("\n=== Codex ===")
        if result["codex"]:
            model = result["codex"]["model"]
            limit = result["codex"]["model_auto_compact_token_limit"]
            print(f"  model: {model}")
            print(f"  model_auto_compact_token_limit: {limit}")
            print(f"  Source: {result['codex']['source']}")
        else:
            print("  model_auto_compact_token_limit: not set")

        if result["recommendation"]:
            print("\n=== Recommendation (based on --context-tokens) ===")
            ctx = result["recommendation"]["context_window"]
            rec_window = result["recommendation"]["recommended_autoCompactWindow"]
            print(f"  Context window: {ctx} tokens")
            print(f"  Recommended autoCompactWindow: {rec_window}")
            print(f"  Triggers at: {((rec_window - 33000) / ctx * 100):.1f}% of context")
            print(f"  Rationale: {result['recommendation']['rationale']}")

        if result["flags"]:
            print("\n=== Flags ===")
            for flag in result["flags"]:
                print(f"  WARNING: {flag}")

        if result["errors"]:
            print("\n=== Errors ===")
            for err in result["errors"]:
                print(f"  ERROR: {err}")

    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
