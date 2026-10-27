"""Offline: readback recognises letters Desktop merged into one input, never a mention."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"
spec = importlib.util.spec_from_file_location("crosstalk_merged_letters_core", KIT / "crosstalk_p0_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


class MergedLetterTests(unittest.TestCase):
    def test_each_letter_in_a_merged_input_is_recognised(self):
        merged = ("Crosstalk message from Codex (a)\nfirst\nref CTP0-aaa\n"
                  "Crosstalk message from Codex (b)\nsecond\nref CTP0-bbb")
        self.assertTrue(core._has_ref(merged, "CTP0-aaa"))
        self.assertTrue(core._has_ref(merged, "CTP0-bbb"))

    def test_mentions_and_prefixes_do_not_count(self):
        self.assertFalse(core._has_ref("did ref CTP0-aaa arrive?", "CTP0-aaa"))
        self.assertFalse(core._has_ref("Owner note\nref CTP0-aaa\nCrosstalk message from x", "CTP0-aaa"))
        self.assertFalse(core._has_ref("Crosstalk message from Codex (a)\nhi\nref CTP0-aaa", "CTP0-aa"))
        self.assertFalse(core._has_ref("Crosstalk message from Codex (a)\nref CTP0-aaab\n"
                                       "Crosstalk message from Codex (b)\nref CTP0-c", "CTP0-aaa"))


if __name__ == "__main__":
    unittest.main()
