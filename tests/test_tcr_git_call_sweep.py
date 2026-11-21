"""Tests that, whichever git command of a whole revert event fails, by exit code or by timeout, the notice says the work was reverted if and only if the reset command succeeded, and lists as restored exactly the files of the first listing that the second does not list.

Copyright (c) 2026 benomahony. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import unittest

import paths
import discovery
import engine
import test_tcr_revert_faults as faults
from checkpoint_repository import git
from git_faults import EXITS_WITH_AN_ERROR, FAULTS, TIMES_OUT_AFTER_RUNNING, NthGitCall
from tcr_hardening_case import HardeningCase
from test_tcr import EDITING

RESET_ARGUMENTS = faults.RESET_COMMAND[1:]
BEFORE_THE_RESET = ["pyproject.toml", "second.txt"]
MUTATING_COMMANDS = frozenset({"reset", "clean", "update-ref"})
NAMES_AFTER = r"{label}: ((?:`[^`]+`(?:, )?)+)\."
COMMANDS_BEFORE_THE_RESET = 52
GIT_COMMANDS_OF_THE_EVENT = 59


def told_of(output: dict[str, object]) -> str:
    """What the agent reads in the hook's output: the context, and the system message."""
    context = output.get("hookSpecificOutput", {})
    text = str(context.get("additionalContext", "")) if isinstance(context, dict) else ""
    return text + str(output.get("systemMessage", ""))


def named_after(told: str, label: str) -> list[str]:
    """The names a notice lists after label, or none when it has no such sentence."""
    listed = re.search(NAMES_AFTER.format(label=re.escape(label)), told)
    return re.findall(r"`([^`]+)`", listed.group(1)) if listed else []


class SweepCase(HardeningCase):
    """What the sweeps share: the start of a revert, the event, and the check of a notice against the tree."""

    with_a_staged_copy = False

    start_a_revert = faults.RevertFaultTests.start_a_revert

    snapshot = faults.RevertFaultTests.snapshot

    def run_event(self) -> dict[str, object]:
        """The output of the hook for the edit, or the message of the error that the hook's entry point tells in its place."""
        try:
            return self.call_here("PostToolUse", **EDITING)
        except discovery.ConfigurationError as error:
            return json.loads(engine.hook_error(error))

    def reset_succeeded(self, calls: NthGitCall) -> bool:
        """Whether the reset command ran and the fault did not hit it."""
        positions = [position for position, argv in enumerate(calls.commands, start=1) if argv[1:5] == RESET_ARGUMENTS]
        return bool(positions) and calls.k != positions[0]

    def still_changed_in_the_tree(self) -> list[str]:
        """The files `git status` shows as changed."""
        return [*git(self.cwd, ["diff", "--name-only", "--no-renames", "HEAD"]).splitlines(), *git(self.cwd, ["ls-files", "--others", "--exclude-standard"]).splitlines()]

    def assert_the_notice_matches_the_tree(self, told: str, done: bool, label: str) -> None:
        """`TCR reverted` is told only after a reset that git reported as done; each sentence that names files agrees with `git status`: the files listed as restored or as now as HEAD has them are clean, and those listed as still changed, or as changed after TCR listed the files, are changed."""
        self.assertEqual("TCR reverted" in told, done, f"{label}: the reset {'succeeded' if done else 'did not succeed or is not known to have'}: {told!r}")
        remaining = self.still_changed_in_the_tree()
        restored = named_after(told, "Restored")
        self.assertEqual(sorted(restored), sorted(name for name in BEFORE_THE_RESET if name not in remaining) if restored else [], f"{label}: the files listed as restored are not the files that came back: {told!r}")
        self.assertEqual([name for name in named_after(told, "Now as HEAD has them") if name in remaining], [], f"{label}: a file listed as now as HEAD has it is changed: {told!r}")
        self.assertEqual([name for name in named_after(told, "Still changed") if name not in remaining], [], f"{label}: a file listed as still changed is clean: {told!r}")
        self.assertEqual([name for name in named_after(told, "Changed after TCR listed the files") if name not in remaining], [], f"{label}: a file listed as changed after the listing is clean: {told!r}")

    def sweep(self, faults_to_run: tuple[str, ...], only: frozenset[str] | None = None) -> int:
        """Runs the event once per fault and command, or only per command that git runs with a name in only, each from the same start, and checks the notice against the tree; returns how many commands the event runs."""
        self.start_a_revert()
        restore = self.snapshot()
        with NthGitCall(None, EXITS_WITH_AN_ERROR) as counting, contextlib.redirect_stderr(io.StringIO()):
            self.run_event()
        total = len(counting.commands)
        positions = [position for position, argv in enumerate(counting.commands, start=1) if only is None or argv[1] in only]
        assert positions, f"no command of the event is one of {sorted(only or [])}"
        for fault in faults_to_run:
            for k in positions:
                restore()
                with self.subTest(fault=fault, command=k), NthGitCall(k, fault) as failing, contextlib.redirect_stderr(io.StringIO()):
                    output = self.run_event()
                    self.assertEqual(json.loads(json.dumps(output)), output, "the output does not survive JSON")
                    self.assert_the_notice_matches_the_tree(told_of(output), self.reset_succeeded(failing), f"the {k}th command {fault}")
        return total


class EveryGitCallOfARevertTests(SweepCase):
    def test_a_revert_runs_no_more_than_the_known_number_of_git_commands_before_the_reset(self) -> None:
        self.start_a_revert()
        with NthGitCall(None, EXITS_WITH_AN_ERROR) as counting, contextlib.redirect_stderr(io.StringIO()):
            self.run_event()
        position = next(position for position, argv in enumerate(counting.commands) if argv[1:5] == RESET_ARGUMENTS)
        self.assertLessEqual(position, COMMANDS_BEFORE_THE_RESET, f"{position} git commands ran before the reset, where {COMMANDS_BEFORE_THE_RESET} ran when this was written")

    def test_whichever_git_command_fails_the_notice_says_reverted_only_when_the_reset_succeeded_and_names_files_as_git_status_shows_them(self) -> None:
        """Every git command of the event fails once by an exit code and once by a timeout, and each run keeps the invariants: valid JSON, `TCR reverted` only after a reset that git ran, and each sentence that names files agrees with `git status`."""
        total = self.sweep(FAULTS)
        self.assertEqual(total, GIT_COMMANDS_OF_THE_EVENT, "the event runs another number of git commands; the sweep covers each, so check what changed")

    def test_a_command_that_changes_the_tree_and_then_times_out_gives_a_notice_that_says_reverted_only_when_it_was_not_the_reset(self) -> None:
        """The reset, the clean and the update of a ref each run to their end and then time out, as a command does that finishes just as its time is up."""
        self.assertGreater(self.sweep((TIMES_OUT_AFTER_RUNNING,), MUTATING_COMMANDS), 0, "the event ran no git command")


class EveryGitCallOfARevertWithNoSavedCopyTests(SweepCase):
    """The same event when `saveReverted` is false and the index holds a version of the new file that the tree does not: nothing is saved, so no ref is told."""

    with_a_staged_copy = True
    project_settings = {"tcr": {"saveReverted": False}}

    def test_whichever_git_command_exits_with_an_error_the_notice_names_files_as_git_status_shows_them(self) -> None:
        self.assertGreater(self.sweep((EXITS_WITH_AN_ERROR,)), 0, "the event ran no git command")


if __name__ == "__main__":
    unittest.main()
