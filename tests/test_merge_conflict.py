"""Tests of how overwatch lists a workspace's files, and how the Codex hook answers, while a merge is in conflict: git
lists a conflicted file once for each stage of the index, and the file must still be listed once.

Copyright (c) 2026 benomahony. SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
import unittest

from codex_hook import CodexHook, write
import workspace

BASE = "base\n"
BINARY_BASE = b"\x00base\x01"
BINARY_THEIRS = b"\x00theirs\x02"
BINARY_OURS = b"\x00ours\x03"


def remove(path: Path) -> None:
    """Deletes the file at path."""
    path.unlink()


def rename(path: Path, name: str) -> None:
    """Renames the file at path to name, beside it."""
    path.rename(path.with_name(name))


def write_binary(path: Path, data: bytes) -> None:
    """Writes data to the file at path, replacing what it held."""
    path.write_bytes(data)


def make_directory(path: Path) -> None:
    """Creates the directory at path."""
    path.mkdir()


class MergeConflictTests(CodexHook):
    def merge(self, *args: str) -> None:
        """Runs a git command that leaves a conflicted merge in the project, which exits 1 by design."""
        subprocess.run(["git", "-c", "user.email=dev@example.com", "-c", "user.name=dev", *args],
                       cwd=self.cwd, capture_output=True)

    def conflict(self, prepare: Callable[[Path], None], theirs: Callable[[Path], None], ours: Callable[[Path], None], *, repeats: bool = True) -> list[str]:
        """Commits the project's base after prepare, changes the other branch with theirs and this one with ours, merges
        the other branch, and returns the files that stay unmerged, which the test relies on to be a real conflict. A test
        that needs git to list a file more than once, as a conflict on one path does, leaves repeats set."""
        write(self.cwd / "plain.txt", BASE)
        prepare(self.cwd)
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        self.git("checkout", "-qb", "other")
        theirs(self.cwd)
        self.git("add", "-A")
        self.git("commit", "-qm", "theirs")
        self.git("checkout", "-q", "-")
        ours(self.cwd)
        self.git("add", "-A")
        self.git("commit", "-qm", "ours")
        self.merge("merge", "other")
        stages = subprocess.run(["git", "ls-files", "-u", "-z"], cwd=self.cwd, capture_output=True, check=True).stdout
        unmerged = sorted({entry.split(b"\t", 1)[1].decode() for entry in stages.split(b"\0") if entry})
        assert unmerged, f"the merge in {self.cwd} left no unmerged file, so it tests nothing; make the two branches change the same file"
        assert not repeats or len(stages.split(b"\0")) > len(unmerged) + 1, f"git listed each of {unmerged} once in {self.cwd}; a conflict lists one entry per stage"
        return unmerged

    def listed(self) -> list[str]:
        """The files workspace_files lists, relative to the project and in the order it lists them."""
        return [str(path.relative_to(self.cwd)) for path in workspace.workspace_files(self.cwd)]

    def assert_each_listed_once(self, unmerged: list[str], untracked: list[str]) -> None:
        listed = self.listed()
        self.assertEqual(len(listed), len(set(listed)), f"{listed} lists a file twice")
        self.assertEqual(sorted(listed), sorted({"plain.txt", *unmerged, *untracked, *self.tracked_beyond(unmerged)}))
        self.assertEqual(listed, self.listed())

    def tracked_beyond(self, unmerged: list[str]) -> list[str]:
        """The tracked files that the merge did not leave unmerged, other than plain.txt, which every test has."""
        names = subprocess.run(["git", "ls-files", "-z"], cwd=self.cwd, capture_output=True, check=True).stdout
        return [name.decode() for name in names.split(b"\0") if name and name.decode() not in unmerged and name != b"plain.txt"]

    def test_a_file_both_branches_modified_is_listed_once(self) -> None:
        unmerged = self.conflict(
            lambda cwd: write(cwd / "f.txt", BASE),
            lambda cwd: write(cwd / "f.txt", "theirs\n", BASE),
            lambda cwd: write(cwd / "f.txt", "ours\n", BASE),
        )
        self.assertEqual(unmerged, ["f.txt"])
        self.assert_each_listed_once(unmerged, [])

    def test_a_file_both_branches_added_is_listed_once(self) -> None:
        unmerged = self.conflict(
            lambda cwd: None,
            lambda cwd: write(cwd / "a.txt", "theirs\n"),
            lambda cwd: write(cwd / "a.txt", "ours\n"),
        )
        self.assertEqual(unmerged, ["a.txt"])
        self.assert_each_listed_once(unmerged, [])

    def test_a_file_one_branch_deleted_and_the_other_modified_is_listed_once(self) -> None:
        unmerged = self.conflict(
            lambda cwd: write(cwd / "f.txt", BASE),
            lambda cwd: remove(cwd / "f.txt"),
            lambda cwd: write(cwd / "f.txt", "ours\n", BASE),
        )
        self.assertEqual(unmerged, ["f.txt"])
        self.assert_each_listed_once(unmerged, [])

    def test_a_file_each_branch_renamed_differently_is_listed_once_by_each_name(self) -> None:
        unmerged = self.conflict(
            lambda cwd: write(cwd / "f.txt", "one\ntwo\nthree\nfour\nfive\n"),
            lambda cwd: rename(cwd / "f.txt", "theirs.txt"),
            lambda cwd: rename(cwd / "f.txt", "ours.txt"),
            repeats=False,
        )
        self.assertTrue(unmerged)
        self.assert_each_listed_once(unmerged, [])

    def test_a_binary_file_both_branches_changed_is_listed_once(self) -> None:
        unmerged = self.conflict(
            lambda cwd: write_binary(cwd / "b.bin", BINARY_BASE),
            lambda cwd: write_binary(cwd / "b.bin", BINARY_THEIRS),
            lambda cwd: write_binary(cwd / "b.bin", BINARY_OURS),
        )
        self.assertEqual(unmerged, ["b.bin"])
        self.assert_each_listed_once(unmerged, [])

    def test_several_conflicted_files_and_untracked_files_are_each_listed_once(self) -> None:
        def prepare(cwd: Path) -> None:
            for name in ("one.txt", "two.txt", "three.txt"):
                write(cwd / name, BASE)

        def theirs(cwd: Path) -> None:
            for name in ("one.txt", "two.txt"):
                write(cwd / name, "theirs\n", BASE)

        def ours(cwd: Path) -> None:
            for name in ("one.txt", "two.txt"):
                write(cwd / name, "ours\n", BASE)
            write(cwd / "three.txt", "ours only\n", BASE)

        unmerged = self.conflict(prepare, theirs, ours)
        write(self.cwd / "untracked.txt", "new\n")
        make_directory(self.cwd / "dir")
        write(self.cwd / "dir" / "untracked-too.txt", "new\n")
        self.assertEqual(unmerged, ["one.txt", "two.txt"])
        self.assert_each_listed_once(unmerged, ["untracked.txt", "dir/untracked-too.txt"])

    def test_the_hook_answers_during_a_merge_as_it_does_once_the_merge_is_aborted(self) -> None:
        result, _ = self.configure()
        write(result, "RESULT: PASS")
        self.git("add", "-A")
        self.git("commit", "-qm", "init")
        self.call("SessionStart")
        unmerged = self.conflict(
            lambda cwd: write(cwd / "f.txt", BASE),
            lambda cwd: write(cwd / "f.txt", "theirs\n", BASE),
            lambda cwd: write(cwd / "f.txt", "ours\n", BASE),
        )
        during, _ = self.call("PostToolUse", tool_name="Bash")
        stopped_during, _ = self.call("Stop")
        self.git("merge", "--abort")
        after, _ = self.call("PostToolUse", tool_name="Bash")
        stopped_after, _ = self.call("Stop")
        self.assertEqual(unmerged, ["f.txt"])
        self.assertEqual(during, after)
        self.assertEqual(stopped_during, stopped_after)

    def test_the_hook_still_reports_a_new_failure_during_a_merge(self) -> None:
        result, _ = self.configure()
        write(result, "RESULT: PASS")
        self.git("add", "-A")
        self.git("commit", "-qm", "init")
        unmerged = self.conflict(
            lambda cwd: write(cwd / "f.txt", BASE),
            lambda cwd: write(cwd / "f.txt", "theirs\n", BASE),
            lambda cwd: write(cwd / "f.txt", "ours\n", BASE),
        )
        self.call("SessionStart")
        write(result, "RESULT: FAIL", "RESULT: PASS")
        failed, _ = self.call("PostToolUse", tool_name="Bash")
        self.assertEqual(unmerged, ["f.txt"])
        self.assertIn("Full output:", failed["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.call("Stop")[0]["decision"], "block")


if __name__ == "__main__":
    unittest.main()
