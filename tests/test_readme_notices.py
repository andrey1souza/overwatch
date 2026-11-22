"""Tests of what the notices say, as the README does: when a revert is told as one, how files are named, and what is written as an escape, and which notice the plugin writes.

Copyright (c) 2026 benomahony. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import re
import unittest

import paths
import safe_text
import tcr_texts
from checkpoint import RESET_FAILED, RESET_UNKNOWN
SAVED_REF = "refs/overwatch/reverted/x"
CHARACTERS_OF_THE_SUMMARY_EXCEPTION = {"ESC": "\x1b", "NBSP": "\u00a0", "ZWSP": "\u200b", "bidirectional control": "\u202e", "line break": "\n"}


class ReadmeNoticeTests(unittest.TestCase):
    def test_a_reset_nobody_knows_to_have_ended_is_told_as_an_attempt_with_no_claim_about_the_files(self) -> None:
        facts = tcr_texts.Facts(RESET_UNKNOWN, ["a.txt"], ["a.txt"])
        told = tcr_texts.interrupted_attempt(facts, tcr_texts.Recovery(SAVED_REF, False, []))
        self.assertIn("TCR tried to revert", told, "a reset nobody knows to have ended is told as a revert")
        self.assertNotIn("unchanged", told, "the notice claims the files are unchanged")

    def test_an_attempt_lists_the_files_the_second_listing_lacks_as_now_as_head_has_them_and_not_as_restored(self) -> None:
        facts = tcr_texts.Facts(RESET_FAILED, ["a.txt", "b.txt"], ["b.txt"])
        told = tcr_texts.interrupted_attempt(facts, tcr_texts.Recovery(SAVED_REF, False, []))
        self.assertIn("Now as HEAD has them: `a.txt`.", told, "the notice does not say which file is as HEAD has it")
        self.assertIn("Still changed: `b.txt`.", told, "the notice does not say which file is still changed")
        self.assertNotIn("Restored", told, "the notice of an attempt says a file was restored")

    def test_a_summary_with_a_raw_esc_nbsp_zwsp_bidirectional_control_or_line_break_is_written_with_escapes(self) -> None:
        for kind, character in CHARACTERS_OF_THE_SUMMARY_EXCEPTION.items():
            with self.subTest(kind):
                written = safe_text.safe_line(f"a{character}b")
                self.assertNotIn(character, written, f"{kind} is written raw")
                self.assertEqual(written.startswith("a\\") and written.endswith("b"), True, f"{kind} is not written as an escape: {written!r}")


CHANGED_WHILE_ACTING = re.compile(r"export const CHANGED_WHILE_ACTING = '([^']+)'")
FIRST_EDIT_BEFORE_THE_DECISION = "The first edit of a turn can land before the turn start has finished asking whether TCR can run."


def tells_the_notice(readme: str, notice: str) -> bool:
    section = readme.split(FIRST_EDIT_BEFORE_THE_DECISION, 1)[1].split("TCR starts again", 1)[0]
    return f'"{notice}"' in section


NET_FILES = "tcr-net-*.test.ts"
SWEEPS_ENTRIES = "sweepEntries("
ENTRY_NAME = re.compile(r"^    name: ", re.M)
NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}


def net_files() -> list[str]:
    """The test files that sweep the entries the host calls, found by their call of sweepEntries."""
    return sorted(path.name for path in (paths.ROOT / "tests").glob(NET_FILES) if SWEEPS_ENTRIES in path.read_text())


def net_entries() -> int:
    return sum(len(ENTRY_NAME.findall((paths.ROOT / "tests" / name).read_text())) for name in net_files())


def says_the_net_files(readme: str, files: list[str], entries: int) -> bool:
    count = f"in {NUMBER_WORDS[entries]} entries over {NUMBER_WORDS[len(files)]} files"
    return count in readme and all(f"`tests/{name}`" in readme for name in files)


def written_notice() -> str:
    found = CHANGED_WHILE_ACTING.search((paths.ROOT / "hooks" / "pausing.ts").read_text())
    assert found is not None, "pausing.ts no longer exports CHANGED_WHILE_ACTING as one quoted text"
    return found.group(1)


class ReadmeHookNoticeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.readme = " ".join((paths.ROOT / "README.md").read_text().split())

    def test_the_first_edit_before_the_decision_is_described_with_the_notice_the_plugin_writes(self) -> None:
        self.assertTrue(tells_the_notice(self.readme, written_notice()))

    def test_the_check_fails_when_the_readme_says_another_notice(self) -> None:
        other = self.readme.replace(written_notice(), "the working tree has uncommitted changes")
        self.assertFalse(tells_the_notice(other, written_notice()))

    def test_the_readme_names_the_files_that_sweep_the_entries_and_how_many_entries_they_hold(self) -> None:
        self.assertTrue(says_the_net_files(self.readme, net_files(), net_entries()))

    def test_the_check_fails_when_the_readme_names_one_file_of_the_sweep_fewer_or_another_count(self) -> None:
        files = net_files()
        self.assertFalse(says_the_net_files(self.readme.replace("`tests/tcr-net-gates.test.ts`, ", ""), files, net_entries()))
        self.assertFalse(says_the_net_files(self.readme.replace("in seven entries", "in two entries"), files, net_entries()))

    def test_the_sweep_has_the_seven_entries_over_four_files_the_readme_says(self) -> None:
        self.assertEqual((len(net_files()), net_entries()), (4, 7))


if __name__ == "__main__":
    unittest.main()
