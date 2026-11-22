"""The Python side of the differences between a gate pattern in Python and in the plugin that the README lists, so that the README's account of what Python does is checked and not only that of the plugin.

Copyright (c) 2026 benomahony. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import re
import unittest

ARABIC_INDIC_THREE = "٣"
E_ACUTE = "é"


class GatePatternDifferenceTests(unittest.TestCase):
    def test_python_ends_the_text_before_a_last_line_break_and_not_before_another(self) -> None:
        self.assertTrue(re.search("^git push$", "git push\n"))
        self.assertTrue(re.search("^git push$", "git push"))
        self.assertFalse(re.search("^git push$", "git push\nls"))

    def test_python_reads_unicode_digits_letters_and_word_boundaries(self) -> None:
        self.assertTrue(re.search(r"^rm \d$", f"rm {ARABIC_INDIC_THREE}"))
        self.assertTrue(re.search(r"^rm \w+$", f"rm {E_ACUTE}"))
        self.assertIsNone(re.search(rf"rm\b{E_ACUTE}", f"rm{E_ACUTE}"))


if __name__ == "__main__":
    unittest.main()
