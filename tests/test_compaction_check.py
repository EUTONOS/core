"""Tests for tools/compaction_check.py"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Import the module to test
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools.compaction_check import main


class TestCompactionCheck(unittest.TestCase):
    """Test cases for compaction_check.py"""

    def setUp(self):
        """Create temporary home and root directories for each test."""
        self.temp_home = tempfile.TemporaryDirectory()
        self.temp_root = tempfile.TemporaryDirectory()
        self.home = self.temp_home.name
        self.root = self.temp_root.name
        self.addCleanup(self.temp_home.cleanup)
        self.addCleanup(self.temp_root.cleanup)

    def test_no_files_present(self):
        """Test when no settings files exist."""
        result = main(["--home", self.home, "--root", self.root])
        self.assertEqual(result, 0)

    def test_claude_user_settings(self):
        """Test reading Claude settings from user home."""
        # Create user .claude/settings.json
        claude_dir = os.path.join(self.home, ".claude")
        os.makedirs(claude_dir)
        settings_path = os.path.join(claude_dir, "settings.json")
        with open(settings_path, "w") as f:
            json.dump({"autoCompactWindow": 289000, "theme": "dark"}, f)

        # Run and capture output
        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--home", self.home, "--root", self.root])
        self.assertEqual(result, 0)
        out_text = output.getvalue()
        self.assertIn("289000", out_text)
        self.assertIn("Trigger estimate", out_text)

    def test_claude_project_override(self):
        """Test that project settings override user settings."""
        # Create user settings
        user_claude = os.path.join(self.home, ".claude")
        os.makedirs(user_claude)
        with open(os.path.join(user_claude, "settings.json"), "w") as f:
            json.dump({"autoCompactWindow": 200000}, f)

        # Create project settings (should override)
        project_claude = os.path.join(self.root, ".claude")
        os.makedirs(project_claude)
        with open(os.path.join(project_claude, "settings.json"), "w") as f:
            json.dump({"autoCompactWindow": 350000}, f)

        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--home", self.home, "--root", self.root])
        self.assertEqual(result, 0)
        out_text = output.getvalue()
        # Should show the project value, not user value
        self.assertIn("350000", out_text)

    def test_claude_local_override(self):
        """Test that .local settings override both user and project."""
        # Create user, project, and local settings
        user_claude = os.path.join(self.home, ".claude")
        os.makedirs(user_claude)
        with open(os.path.join(user_claude, "settings.json"), "w") as f:
            json.dump({"autoCompactWindow": 200000}, f)

        project_claude = os.path.join(self.root, ".claude")
        os.makedirs(project_claude)
        with open(os.path.join(project_claude, "settings.json"), "w") as f:
            json.dump({"autoCompactWindow": 350000}, f)
        with open(os.path.join(project_claude, "settings.local.json"), "w") as f:
            json.dump({"autoCompactWindow": 400000}, f)

        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--home", self.home, "--root", self.root])
        self.assertEqual(result, 0)
        out_text = output.getvalue()
        self.assertIn("400000", out_text)
        self.assertIn("local", out_text)

    def test_codex_toml_reading(self):
        """Test reading Codex config.toml."""
        # Create .codex/config.toml
        codex_dir = os.path.join(self.home, ".codex")
        os.makedirs(codex_dir)
        toml_path = os.path.join(codex_dir, "config.toml")
        with open(toml_path, "w") as f:
            f.write('model = "gpt-6-astra"\n')
            f.write("model_auto_compact_token_limit = 256000\n")

        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--home", self.home, "--root", self.root])
        self.assertEqual(result, 0)
        out_text = output.getvalue()
        self.assertIn("gpt-6-astra", out_text)
        self.assertIn("256000", out_text)

    def test_trigger_estimate_calculation(self):
        """Test that trigger estimate is correctly calculated (window - 33000)."""
        user_claude = os.path.join(self.home, ".claude")
        os.makedirs(user_claude)
        with open(os.path.join(user_claude, "settings.json"), "w") as f:
            json.dump({"autoCompactWindow": 100000}, f)

        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--home", self.home, "--root", self.root])
        self.assertEqual(result, 0)
        out_text = output.getvalue()
        # 100000 - 33000 = 67000
        self.assertIn("67000", out_text)

    def test_recommendation_at_88_percent(self):
        """Test recommendation calculation: window = round(0.88*N) + 33000."""
        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--home", self.home, "--root", self.root, "--context-tokens", "1000000"])
        self.assertEqual(result, 0)
        out_text = output.getvalue()
        # 0.88 * 1000000 = 880000; round(880000) + 33000 = 913000
        self.assertIn("913000", out_text)
        self.assertIn("88", out_text)  # Should mention 88% somewhere

    def test_recommendation_cap_at_context_window(self):
        """Test that recommendation doesn't exceed context window."""
        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            # Use a small context window
            result = main(["--home", self.home, "--root", self.root, "--context-tokens", "100000"])
        self.assertEqual(result, 0)
        out_text = output.getvalue()
        # Calculated: round(0.88 * 100000) + 33000 = round(88000) + 33000 = 121000
        # But capped to 100000 (context window)
        self.assertIn("100000", out_text)

    def test_flag_above_95_percent(self):
        """Test flag when current setting triggers above 95% of context."""
        # Set Claude window to trigger at 96% of a 1M token context
        # trigger = 0.96 * 1000000 + 33000 = 993000
        user_claude = os.path.join(self.home, ".claude")
        os.makedirs(user_claude)
        with open(os.path.join(user_claude, "settings.json"), "w") as f:
            json.dump({"autoCompactWindow": 993000}, f)

        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--home", self.home, "--root", self.root, "--context-tokens", "1000000"])
        # Should have a warning flag
        out_text = output.getvalue()
        self.assertIn("WARNING", out_text)
        self.assertIn("95", out_text)

    def test_flag_below_60_percent(self):
        """Test flag when current setting triggers below 60% of context."""
        # Set Claude window to trigger at 50% of a 1M token context
        # trigger = 0.50 * 1000000 + 33000 = 533000
        user_claude = os.path.join(self.home, ".claude")
        os.makedirs(user_claude)
        with open(os.path.join(user_claude, "settings.json"), "w") as f:
            json.dump({"autoCompactWindow": 533000}, f)

        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--home", self.home, "--root", self.root, "--context-tokens", "1000000"])
        out_text = output.getvalue()
        self.assertIn("WARNING", out_text)
        self.assertIn("60", out_text)

    def test_malformed_json(self):
        """Test handling of malformed JSON in settings."""
        user_claude = os.path.join(self.home, ".claude")
        os.makedirs(user_claude)
        with open(os.path.join(user_claude, "settings.json"), "w") as f:
            f.write("{ invalid json")

        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--home", self.home, "--root", self.root])
        out_text = output.getvalue()
        self.assertIn("ERROR", out_text)
        self.assertIn("parse", out_text.lower())
        self.assertEqual(result, 1)

    def test_malformed_toml(self):
        """Test handling of malformed TOML in config."""
        codex_dir = os.path.join(self.home, ".codex")
        os.makedirs(codex_dir)
        with open(os.path.join(codex_dir, "config.toml"), "w") as f:
            f.write("[invalid toml\n")

        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--home", self.home, "--root", self.root])
        out_text = output.getvalue()
        self.assertIn("ERROR", out_text)

    def test_json_output_format(self):
        """Test --json output format."""
        user_claude = os.path.join(self.home, ".claude")
        os.makedirs(user_claude)
        with open(os.path.join(user_claude, "settings.json"), "w") as f:
            json.dump({"autoCompactWindow": 289000}, f)

        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--home", self.home, "--root", self.root, "--json"])
        self.assertEqual(result, 0)
        out_text = output.getvalue()
        # Should be valid JSON
        try:
            parsed = json.loads(out_text)
            self.assertIn("claude", parsed)
        except json.JSONDecodeError:
            self.fail("--json output is not valid JSON")

    def test_json_with_recommendation(self):
        """Test --json output includes recommendation."""
        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "--home",
                    self.home,
                    "--root",
                    self.root,
                    "--context-tokens",
                    "1000000",
                    "--json",
                ]
            )
        self.assertEqual(result, 0)
        out_text = output.getvalue()
        parsed = json.loads(out_text)
        self.assertIn("recommendation", parsed)
        self.assertEqual(parsed["recommendation"]["context_window"], 1000000)

    def test_empty_settings_files(self):
        """Test with empty settings objects."""
        user_claude = os.path.join(self.home, ".claude")
        os.makedirs(user_claude)
        with open(os.path.join(user_claude, "settings.json"), "w") as f:
            json.dump({}, f)

        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--home", self.home, "--root", self.root])
        self.assertEqual(result, 0)
        out_text = output.getvalue()
        self.assertIn("not set", out_text)

    def test_recommendation_needs_context_tokens(self):
        """Test that recommendation requires --context-tokens."""
        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--home", self.home, "--root", self.root])
        self.assertEqual(result, 0)
        out_text = output.getvalue()
        # Without --context-tokens, no recommendation should appear
        self.assertNotIn("Recommendation", out_text)


if __name__ == "__main__":
    unittest.main()
