"""Tests of the TCR checkpoint and revert, run against real git repositories in temporary directories.

Copyright (c) 2026 benomahony. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from paths import ROOT
import checkpoint
from codex_hook import read
from workspace import is_committed

HOOK_MODE = 0o755
# What git leaves in its directory while an operation is unfinished, and what that operation is called.
UNFINISHED = {"MERGE_HEAD": "merge", "CHERRY_PICK_HEAD": "cherry-pick", "REVERT_HEAD": "revert", "BISECT_LOG": "bisect",
              "rebase-merge": "rebase", "rebase-apply": "rebase"}
# Git ignores the machine's own configuration, so its signing and global hooks do not run inside these tests.
GIT_ENVIRONMENT = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def set_environment(values: dict[str, str]) -> dict[str, str | None]:
    """Sets values in this process's environment and returns what each variable held before, for restore_git."""
    saved = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    assert all(os.environ[name] == value for name, value in values.items()), f"the environment is {dict(os.environ)}, not {values}"
    assert set(saved) == set(values), f"saved {set(saved)} but set {set(values)}; save every variable"
    return saved


def isolate_git() -> dict[str, str | None]:
    """Sets GIT_ENVIRONMENT in this process and returns what each variable held before, for restore_git."""
    saved = set_environment(GIT_ENVIRONMENT)
    assert os.environ["GIT_CONFIG_GLOBAL"] == "/dev/null", "git would still read the machine's configuration"
    assert set(saved) == set(GIT_ENVIRONMENT), f"saved {set(saved)} but isolated {set(GIT_ENVIRONMENT)}"
    return saved


def restore_git(saved: dict[str, str | None]) -> None:
    """Puts back what isolate_git found: each variable's old value, or no variable where there was none."""
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    assert all(os.environ.get(name) == value for name, value in saved.items()), f"git's environment was not restored to {saved}"
    assert all(name in os.environ or saved[name] is None for name in saved), f"a variable of {set(saved)} was lost on the way back"


def git(cwd: Path, argv: list[str]) -> str:
    """Runs git with argv in cwd and returns what it printed. Inside a test the environment is GIT_ENVIRONMENT."""
    assert os.environ.get("GIT_CONFIG_GLOBAL") == "/dev/null", "git would read the machine's configuration; run it inside a test's setUp"
    result = subprocess.run(["git", "-C", str(cwd), *argv], capture_output=True, text=True, check=True)
    assert result.returncode == 0, f"git {argv} exited {result.returncode}: {result.stderr}"
    assert isinstance(result.stdout, str), f"git {argv} printed {type(result.stdout)}, not text"
    return result.stdout.strip()


def put(path: Path, text: str) -> None:
    """Writes text to a fixture file in the test's own temporary directory, replacing what it held."""
    with path.open("w") as handle:
        handle.write(text)
    assert path.stat().st_size == len(text.encode()), f"{path} holds {path.stat().st_size} bytes of the {len(text.encode())} written"
    assert path.read_bytes() == text.encode(), f"{path} reads back differently from what was written"


def present(path: Path) -> bool:
    """Whether a file or directory exists at path."""
    found = path.exists()
    assert isinstance(found, bool), f"existence of {path} came back as {type(found)}, not a yes or no"
    assert found == (path.is_file() or path.is_dir()), f"{path} exists but is neither a file nor a directory"
    return found


def remove(path: Path) -> None:
    """Deletes one fixture file that exists."""
    assert path.is_file(), f"{path} is not a file to remove; remove only files the test made"
    path.unlink()
    assert not path.exists(), f"{path} is still there after it was removed"


def make_dir(path: Path) -> None:
    """Makes a fixture directory that does not exist yet."""
    assert not path.exists(), f"{path} exists already; give each fixture directory its own name"
    path.mkdir(parents=True)
    assert path.is_dir(), f"{path} was not made; check its parent is writable"


def clear_marker(location: Path) -> None:
    """Removes a marker that git leaves in its directory while an operation is unfinished: a file, or an empty directory."""
    assert location.exists(), f"{location} is not there to clear; make the marker first"
    if location.is_dir():
        location.rmdir()
    else:
        location.unlink()
    assert not location.exists(), f"{location} is still there after it was cleared"


def set_mode(path: Path, mode: int) -> None:
    """Sets the permission bits of a fixture file or directory, for tests of what git does when it cannot read one."""
    assert path.exists(), f"{path} is not there to change the mode of"
    path.chmod(mode)
    assert path.stat().st_mode & 0o777 == mode, f"{path} has mode {path.stat().st_mode & 0o777:o}, not {mode:o}"


def install_hook(repo: Path, name: str, body: str) -> None:
    """Installs an executable git hook named name in repo, running body as a shell script."""
    assert body.strip() != "", f"the hook {name} has an empty body; give it a command to run"
    assert (repo / ".git").is_dir(), f"{repo} has no .git directory, so the hook {name} would never run; install hooks in a repository"
    hook = repo / ".git" / "hooks" / name
    put(hook, f"#!/bin/sh\n{body}\n")
    hook.chmod(HOOK_MODE)
    assert os.access(hook, os.X_OK), f"the hook {hook} is not executable; chmod it"


class CheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        """A repository with one committed file, an ignore file, and an ignored log, and a directory outside any
        repository, each removed afterwards."""
        self.addCleanup(restore_git, isolate_git())
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.outside = tempfile.TemporaryDirectory()
        self.addCleanup(self.outside.cleanup)
        git(self.repo, ["init", "-q", "-b", "main"])
        put(self.repo / ".gitignore", "*.log\nnode_modules/\n")
        put(self.repo / "a.txt", "base\n")
        git(self.repo, ["add", "-A"])
        git(self.repo, ["commit", "-q", "-m", "start"])
        put(self.repo / "keep.log", "ignored\n")
        assert git(self.repo, ["status", "--porcelain"]) == "", f"{self.repo} is not clean after setup"
        assert present(self.repo / "keep.log"), f"the ignored file was not written in {self.repo}"

    def test_the_module_ships_inside_the_engine_directory(self) -> None:
        package = json.loads(read(ROOT / "package.json"))
        self.assertIn("engine", package["files"])
        self.assertTrue((ROOT / "engine" / "checkpoint.py").is_file())

    def test_a_clean_tree_is_clean(self) -> None:
        self.assertTrue(checkpoint.is_clean(self.repo))

    def test_an_edited_or_new_file_makes_the_tree_dirty_but_an_ignored_one_does_not(self) -> None:
        put(self.repo / "other.log", "ignored too\n")
        self.assertTrue(checkpoint.is_clean(self.repo))
        put(self.repo / "new.txt", "new\n")
        self.assertFalse(checkpoint.is_clean(self.repo))
        remove(self.repo / "new.txt")
        put(self.repo / "a.txt", "edited\n")
        self.assertFalse(checkpoint.is_clean(self.repo))

    def test_a_checkpoint_commits_every_file_that_is_not_ignored(self) -> None:
        put(self.repo / "a.txt", "edited\n")
        put(self.repo / "new.txt", "new\n")
        put(self.repo / "later.log", "ignored\n")
        sha = checkpoint.checkpoint(self.repo)
        self.assertEqual(sha, git(self.repo, ["rev-parse", "HEAD"]))
        self.assertEqual(git(self.repo, ["status", "--porcelain"]), "")
        self.assertEqual(git(self.repo, ["show", "--name-only", "--format=", "HEAD"]).split(), ["a.txt", "new.txt"])

    def test_the_checkpoint_message_names_the_files_it_changed(self) -> None:
        put(self.repo / "a.txt", "edited\n")
        put(self.repo / "new.txt", "new\n")
        checkpoint.checkpoint(self.repo)
        self.assertEqual(git(self.repo, ["log", "-1", "--format=%s"]), "tcr: a.txt, new.txt")

    def test_a_long_list_of_files_is_cut_in_the_message(self) -> None:
        for index in range(checkpoint.MESSAGE_FILES + 3):
            put(self.repo / f"f{index:02}.txt", "x\n")
        checkpoint.checkpoint(self.repo)
        subject = git(self.repo, ["log", "-1", "--format=%s"])
        self.assertTrue(subject.startswith("tcr: f00.txt"), subject)
        self.assertTrue(subject.endswith("and 3 more"), subject)

    def test_a_checkpoint_with_nothing_to_commit_makes_no_commit(self) -> None:
        before = git(self.repo, ["rev-parse", "HEAD"])
        self.assertIsNone(checkpoint.checkpoint(self.repo))
        self.assertEqual(git(self.repo, ["rev-parse", "HEAD"]), before)

    def test_a_checkpoint_leaves_only_commit_entries_so_the_baseline_survives_it(self) -> None:
        before = git(self.repo, ["rev-parse", "HEAD"])
        put(self.repo / "a.txt", "edited\n")
        checkpoint.checkpoint(self.repo)
        self.assertEqual(git(self.repo, ["reflog", "-1", "--format=%gs"]).split(":")[0], "commit")
        self.assertTrue(is_committed(self.repo, before, git(self.repo, ["rev-parse", "HEAD"])))

    def test_a_revert_restores_tracked_files_and_removes_new_ones_but_keeps_ignored_ones(self) -> None:
        put(self.repo / "a.txt", "broken\n")
        put(self.repo / "agent_new.py", "x\n")
        make_dir(self.repo / "pkg")
        put(self.repo / "pkg" / "m.py", "y\n")
        checkpoint.revert(self.repo)
        self.assertEqual(read(self.repo / "a.txt"), "base\n")
        self.assertFalse(present(self.repo / "agent_new.py"))
        self.assertFalse(present(self.repo / "pkg"))
        self.assertEqual(read(self.repo / "keep.log"), "ignored\n")
        self.assertEqual(git(self.repo, ["status", "--porcelain"]), "")

    def discard_work(self) -> None:
        """Gives the repository a changed tracked file, a deleted tracked file, a new file and a nested folder with a
        new file, besides the ignored log."""
        put(self.repo / "b.txt", "tracked\n")
        git(self.repo, ["add", "b.txt"])
        git(self.repo, ["commit", "-q", "-m", "second"])
        put(self.repo / "a.txt", "broken\n")
        remove(self.repo / "b.txt")
        put(self.repo / "agent_new.py", "x\n")
        make_dir(self.repo / "pkg")
        put(self.repo / "pkg" / "m.py", "y\n")
        assert sorted(checkpoint.uncommitted(self.repo)) == ["a.txt", "agent_new.py", "b.txt", "pkg/m.py"], "the fixture work is not what the tests expect"
        assert present(self.repo / "keep.log"), f"the ignored file is gone from {self.repo} before the revert"

    def revert_leaves_head_and_index(self, save_ref: bool) -> str | None:
        """Discards work, reverts it, and checks that HEAD, the index listing and the status are as a clean tree has
        them; returns the ref the work was saved to."""
        self.discard_work()
        head, listing = git(self.repo, ["rev-parse", "HEAD"]), git(self.repo, ["ls-files", "-s"])
        saved = checkpoint.revert(self.repo, save_ref=save_ref)
        assert git(self.repo, ["rev-parse", "HEAD"]) == head, f"the revert moved HEAD from {head}"
        assert git(self.repo, ["ls-files", "-s"]) == listing, f"the revert changed the index listing {listing!r}"
        assert git(self.repo, ["status", "--porcelain"]) == "", f"the tree is not clean after the revert: {git(self.repo, ['status', '--porcelain'])!r}"
        return saved

    def test_a_revert_that_saves_keeps_the_discarded_work_in_a_ref_without_touching_head_or_the_index(self) -> None:
        self.discard_work()
        head = git(self.repo, ["rev-parse", "HEAD"])
        saved = checkpoint.revert(self.repo, save_ref=True)
        self.assertIsNotNone(saved)
        self.assertTrue(saved.startswith("refs/overwatch/reverted/"), saved)
        self.assertEqual(git(self.repo, ["rev-parse", "HEAD"]), head)
        self.assertEqual(git(self.repo, ["rev-parse", f"{saved}^"]), head)
        self.assertEqual(git(self.repo, ["show", f"{saved}:a.txt"]), "broken")
        self.assertEqual(git(self.repo, ["show", f"{saved}:agent_new.py"]), "x")
        self.assertEqual(git(self.repo, ["show", f"{saved}:pkg/m.py"]), "y")
        self.assertNotIn("b.txt", git(self.repo, ["ls-tree", "-r", "--name-only", saved]).split())
        self.assertNotIn("keep.log", git(self.repo, ["ls-tree", "-r", "--name-only", saved]).split())

    def test_a_revert_that_saves_leaves_head_the_index_and_the_tree_as_a_revert_that_does_not(self) -> None:
        self.assertIsNotNone(self.revert_leaves_head_and_index(save_ref=True))
        self.assertEqual(read(self.repo / "keep.log"), "ignored\n")

    def test_a_revert_that_does_not_save_makes_no_ref(self) -> None:
        self.assertIsNone(self.revert_leaves_head_and_index(save_ref=False))
        self.assertEqual(git(self.repo, ["for-each-ref", "refs/overwatch"]), "")
        self.assertEqual(read(self.repo / "keep.log"), "ignored\n")

    def test_a_revert_saves_by_default(self) -> None:
        self.discard_work()
        self.assertIsNotNone(checkpoint.revert(self.repo))
        self.assertEqual(len(git(self.repo, ["for-each-ref", "refs/overwatch/reverted"]).splitlines()), 1)

    def test_the_saved_ref_restores_the_work_with_one_checkout(self) -> None:
        self.discard_work()
        saved = checkpoint.revert(self.repo, save_ref=True)
        git(self.repo, ["checkout", saved, "--", "."])
        self.assertEqual(read(self.repo / "a.txt"), "broken\n")
        self.assertEqual(read(self.repo / "agent_new.py"), "x\n")
        self.assertEqual(read(self.repo / "pkg" / "m.py"), "y\n")

    def test_a_revert_that_cannot_save_changes_nothing(self) -> None:
        self.discard_work()
        put(self.repo / ".git" / "refs" / "overwatch", "a file where the refs directory should be\n")
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.revert(self.repo, save_ref=True)
        self.assertEqual(read(self.repo / "a.txt"), "broken\n")
        self.assertEqual(read(self.repo / "agent_new.py"), "x\n")
        self.assertEqual(read(self.repo / "pkg" / "m.py"), "y\n")

    def test_a_file_written_after_the_snapshot_and_before_the_reset_is_in_the_saved_ref(self) -> None:
        self.discard_work()
        written: list[str] = []

        def write_late_file() -> None:
            assert not present(self.repo / "late.txt") or written, f"the late file was in {self.repo} before the seam wrote it"
            if not written:
                written.append("late.txt")
                put(self.repo / "late.txt", "written while the work was saved\n")
            assert present(self.repo / "late.txt"), f"the seam did not write the late file in {self.repo}"

        saved = checkpoint.revert(self.repo, after_save=write_late_file)
        self.assertEqual(git(self.repo, ["show", f"{saved}:late.txt"]), "written while the work was saved")
        self.assertEqual(git(self.repo, ["show", f"{saved}:a.txt"]), "broken")
        self.assertFalse(present(self.repo / "late.txt"))
        self.assertEqual(git(self.repo, ["status", "--porcelain"]), "")

    def test_a_file_written_after_the_last_listing_survives_the_revert_and_is_reported_as_left(self) -> None:
        """A revert removes only the untracked files it listed last, so a file written after that stays and is named as left."""
        self.discard_work()
        written: list[str] = []

        def write_after_the_last_listing() -> None:
            assert not written, f"the seam before the discard ran twice in {self.repo}"
            written.append("late.txt")
            put(self.repo / "late.txt", "written after the listing\n")
            make_dir(self.repo / "late_dir")
            put(self.repo / "late_dir" / "inner.txt", "inside a new directory\n")
            assert present(self.repo / "late.txt") and present(self.repo / "late_dir" / "inner.txt"), f"the seam wrote no late files in {self.repo}"

        with self.assertRaises(checkpoint.CheckpointError) as caught:
            checkpoint.revert(self.repo, before_discard=write_after_the_last_listing)
        self.assertIn("late.txt", str(caught.exception))
        self.assertIn("left", str(caught.exception))
        self.assertEqual(read(self.repo / "late.txt"), "written after the listing\n")
        self.assertEqual(read(self.repo / "late_dir" / "inner.txt"), "inside a new directory\n")
        self.assertEqual(read(self.repo / "a.txt"), "base\n")
        self.assertFalse(present(self.repo / "agent_new.py"))
        saved = git(self.repo, ["for-each-ref", "--format=%(refname)", "refs/overwatch/reverted"])
        self.assertNotIn("late.txt", git(self.repo, ["ls-tree", "-r", "--name-only", saved]))

    def test_a_revert_without_saving_also_keeps_a_file_written_after_the_listing(self) -> None:
        self.discard_work()
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.revert(self.repo, save_ref=False, before_discard=lambda: put(self.repo / "late.txt", "late\n"))
        self.assertEqual(read(self.repo / "late.txt"), "late\n")
        self.assertFalse(present(self.repo / "agent_new.py"))

    def test_a_revert_removes_the_directories_of_the_files_it_removed_but_not_a_directory_with_other_files(self) -> None:
        make_dir(self.repo / "new_dir" / "deep")
        put(self.repo / "new_dir" / "deep" / "one.txt", "x\n")
        checkpoint.revert(self.repo)
        self.assertFalse(present(self.repo / "new_dir"))
        make_dir(self.repo / "kept_dir")
        put(self.repo / "kept_dir" / "ignored.log", "ignored\n")
        put(self.repo / "kept_dir" / "two.txt", "x\n")
        checkpoint.revert(self.repo)
        self.assertTrue(present(self.repo / "kept_dir" / "ignored.log"))
        self.assertFalse(present(self.repo / "kept_dir" / "two.txt"))

    def test_a_file_changed_after_the_snapshot_is_saved_as_it_was_changed(self) -> None:
        self.discard_work()
        edits: list[str] = []

        def change_a_discarded_file() -> None:
            assert present(self.repo / "agent_new.py"), f"the file to change was not in {self.repo}"
            if not edits:
                edits.append("changed")
                put(self.repo / "agent_new.py", "changed while the work was saved\n")
            assert len(edits) == 1, f"the file was changed {len(edits)} times, not once"

        saved = checkpoint.revert(self.repo, after_save=change_a_discarded_file)
        self.assertEqual(git(self.repo, ["show", f"{saved}:agent_new.py"]), "changed while the work was saved")

    def test_a_revert_deletes_nothing_when_files_keep_changing_while_the_work_is_saved(self) -> None:
        self.discard_work()
        counter = [0]

        def write_another_file() -> None:
            counter[0] += 1
            put(self.repo / f"flood{counter[0]}.txt", "again\n")
            assert present(self.repo / f"flood{counter[0]}.txt"), f"the seam did not write flood{counter[0]}.txt"
            assert counter[0] <= checkpoint.MAX_SAVE_ATTEMPTS, f"the revert tried {counter[0]} times, more than {checkpoint.MAX_SAVE_ATTEMPTS}"

        with self.assertRaises(checkpoint.CheckpointError) as caught:
            checkpoint.revert(self.repo, after_save=write_another_file)
        self.assertIn("kept changing", str(caught.exception))
        self.assertEqual(read(self.repo / "a.txt"), "broken\n")
        self.assertEqual(read(self.repo / "agent_new.py"), "x\n")
        self.assertTrue(all(present(self.repo / f"flood{number}.txt") for number in range(1, counter[0] + 1)))
        self.assertEqual(git(self.repo, ["for-each-ref", "refs/overwatch"]), "")

    def test_a_git_command_that_fails_while_the_work_is_saved_leaves_no_ref_and_discards_nothing(self) -> None:
        self.discard_work()
        listings = [0]
        listed = checkpoint.uncommitted

        def fail_the_second_listing(root: Path) -> list[str]:
            listings[0] += 1
            assert root.resolve() == self.repo.resolve(), f"the listing was asked for {root}, not {self.repo}"
            assert listings[0] <= checkpoint.MAX_SAVE_ATTEMPTS + 2, f"{listings[0]} listings were asked for after a failure on the second"
            if listings[0] == 2:
                raise checkpoint.CheckpointError("git took longer than 30 seconds")
            return listed(root)

        with unittest.mock.patch.object(checkpoint, "uncommitted", fail_the_second_listing), self.assertRaises(checkpoint.CheckpointError) as caught:
            checkpoint.revert(self.repo)
        self.assertIn("took longer", str(caught.exception))
        self.assertEqual(git(self.repo, ["for-each-ref", "refs/overwatch"]), "")
        self.assertEqual(read(self.repo / "a.txt"), "broken\n")
        self.assertEqual(read(self.repo / "agent_new.py"), "x\n")

    def test_a_revert_that_saves_nothing_new_makes_one_ref_and_tells_the_seam_once_per_check(self) -> None:
        self.discard_work()
        calls = [0]

        def count() -> None:
            calls[0] += 1
            assert calls[0] == 1, f"the seam ran {calls[0]} times for work that did not change"
            assert present(self.repo / "a.txt"), f"the discarded work is gone from {self.repo} before the seam ran"

        checkpoint.revert(self.repo, after_save=count)
        self.assertEqual(calls[0], 1)
        self.assertEqual(len(git(self.repo, ["for-each-ref", "refs/overwatch/reverted"]).splitlines()), 1)

    def test_two_reverts_in_a_row_keep_two_refs(self) -> None:
        put(self.repo / "a.txt", "first\n")
        first = checkpoint.revert(self.repo, save_ref=True)
        put(self.repo / "a.txt", "second\n")
        second = checkpoint.revert(self.repo, save_ref=True)
        self.assertNotEqual(first, second)
        self.assertEqual(git(self.repo, ["show", f"{first}:a.txt"]), "first")
        self.assertEqual(git(self.repo, ["show", f"{second}:a.txt"]), "second")

    def test_a_revert_goes_back_to_the_latest_checkpoint_not_to_the_start(self) -> None:
        put(self.repo / "a.txt", "green\n")
        checkpoint.checkpoint(self.repo)
        put(self.repo / "a.txt", "red\n")
        checkpoint.revert(self.repo)
        self.assertEqual(read(self.repo / "a.txt"), "green\n")

    def test_a_commit_hook_that_refuses_raises_and_leaves_the_work_in_place(self) -> None:
        install_hook(self.repo, "pre-commit", "echo refused >&2\nexit 1")
        put(self.repo / "a.txt", "green work\n")
        before = git(self.repo, ["rev-parse", "HEAD"])
        with self.assertRaises(checkpoint.CheckpointError) as raised:
            checkpoint.checkpoint(self.repo)
        self.assertIn("refused", str(raised.exception))
        self.assertEqual(git(self.repo, ["rev-parse", "HEAD"]), before)
        self.assertEqual(read(self.repo / "a.txt"), "green work\n")

    def start_a_conflicting_merge(self) -> None:
        """Leaves the repository in the middle of a merge whose two sides changed a.txt differently."""
        git(self.repo, ["checkout", "-q", "-b", "other"])
        put(self.repo / "a.txt", "other\n")
        git(self.repo, ["commit", "-qam", "other"])
        git(self.repo, ["checkout", "-q", "main"])
        put(self.repo / "a.txt", "main\n")
        git(self.repo, ["commit", "-qam", "main"])
        subprocess.run(["git", "-C", str(self.repo), "merge", "other"], capture_output=True, text=True, check=False)
        assert present(self.repo / ".git" / "MERGE_HEAD"), f"the merge in {self.repo} did not stop on a conflict"
        assert "<<<<<<<" in read(self.repo / "a.txt"), f"a.txt in {self.repo} holds no conflict markers"

    def test_a_revert_from_a_subdirectory_cleans_the_whole_repository(self) -> None:
        make_dir(self.repo / "sub")
        put(self.repo / "sub" / "kept.txt", "tracked\n")
        git(self.repo, ["add", "-A"])
        git(self.repo, ["commit", "-q", "-m", "add sub"])
        put(self.repo / "root_new.txt", "x\n")
        put(self.repo / "sub" / "sub_new.txt", "x\n")
        checkpoint.revert(self.repo / "sub")
        self.assertFalse(present(self.repo / "root_new.txt"))
        self.assertFalse(present(self.repo / "sub" / "sub_new.txt"))
        self.assertEqual(git(self.repo, ["status", "--porcelain"]), "")

    def test_file_names_are_written_as_they_are_in_the_message(self) -> None:
        put(self.repo / "my file.txt", "x\n")
        put(self.repo / "\u00fcn\u00ef.txt", "x\n")
        checkpoint.checkpoint(self.repo)
        subject = git(self.repo, ["log", "-1", "--format=%s"])
        self.assertIn("my file.txt", subject)
        self.assertIn("\u00fcn\u00ef.txt", subject)
        self.assertNotIn("\\303", subject)

    def test_a_merge_in_progress_is_neither_committed_nor_reverted(self) -> None:
        self.start_a_conflicting_merge()
        with self.assertRaises(checkpoint.CheckpointError) as raised:
            checkpoint.revert(self.repo)
        self.assertIn("merge", str(raised.exception))
        self.assertTrue(present(self.repo / ".git" / "MERGE_HEAD"))
        self.assertIn("<<<<<<<", read(self.repo / "a.txt"))
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.checkpoint(self.repo)
        self.assertTrue(present(self.repo / ".git" / "MERGE_HEAD"))

    def test_an_untracked_nested_repository_is_not_committed_and_survives_a_revert(self) -> None:
        make_dir(self.repo / "nested")
        git(self.repo / "nested", ["init", "-q", "-b", "main"])
        put(self.repo / "nested" / "f.txt", "x\n")
        git(self.repo / "nested", ["add", "-A"])
        git(self.repo / "nested", ["commit", "-q", "-m", "inner"])
        put(self.repo / "agent_new.py", "y\n")
        before = git(self.repo, ["rev-parse", "HEAD"])
        with self.assertRaises(checkpoint.CheckpointError) as raised:
            checkpoint.checkpoint(self.repo)
        self.assertIn("empty link", str(raised.exception))
        self.assertEqual(git(self.repo, ["rev-parse", "HEAD"]), before)
        checkpoint.revert(self.repo)
        self.assertFalse(present(self.repo / "agent_new.py"))
        self.assertTrue(present(self.repo / "nested" / "f.txt"))

    def test_a_revert_with_no_commit_to_restore_changes_nothing(self) -> None:
        root = Path(self.outside.name)
        git(root, ["init", "-q", "-b", "main"])
        put(root / "a.txt", "work\n")
        with self.assertRaises(checkpoint.CheckpointError) as raised:
            checkpoint.revert(root)
        self.assertIn("no commit", str(raised.exception))
        self.assertEqual(read(root / "a.txt"), "work\n")

    def test_tcr_is_available_only_on_a_clean_branch_with_nothing_in_progress(self) -> None:
        self.assertIsNone(checkpoint.unavailable_reason(self.repo))
        put(self.repo / "a.txt", "edited\n")
        self.assertIn("uncommitted", checkpoint.unavailable_reason(self.repo) or "")
        git(self.repo, ["checkout", "-q", "--", "a.txt"])
        git(self.repo, ["checkout", "-q", "--detach"])
        self.assertIn("detached", checkpoint.unavailable_reason(self.repo) or "")
        git(self.repo, ["checkout", "-q", "main"])
        self.start_a_conflicting_merge()
        self.assertIn("merge", checkpoint.unavailable_reason(self.repo) or "")

    def test_every_unfinished_operation_is_refused_by_name(self) -> None:
        """For each marker git leaves while a merge, rebase, cherry-pick, revert or bisect is unfinished, TCR is
        unavailable and names that operation, and a checkpoint and a revert both refuse and keep the work."""
        for marker, name in UNFINISHED.items():
            location = self.repo / ".git" / marker
            if marker.startswith("rebase"):
                make_dir(location)
            else:
                put(location, "x\n")
            with self.subTest(marker=marker):
                self.assertIn(name, checkpoint.unavailable_reason(self.repo) or "")
                put(self.repo / "a.txt", "work in the middle of " + name + "\n")
                with self.assertRaises(checkpoint.CheckpointError):
                    checkpoint.checkpoint(self.repo)
                with self.assertRaises(checkpoint.CheckpointError):
                    checkpoint.revert(self.repo)
                self.assertEqual(read(self.repo / "a.txt"), "work in the middle of " + name + "\n")
            clear_marker(location)
            git(self.repo, ["checkout", "-q", "--", "a.txt"])

    def test_tcr_is_unavailable_in_a_repository_with_no_commit(self) -> None:
        git(Path(self.outside.name), ["init", "-q", "-b", "main"])
        self.assertIn("no commit", checkpoint.unavailable_reason(Path(self.outside.name)) or "")

    def test_tcr_is_unavailable_while_an_untracked_nested_repository_is_present(self) -> None:
        make_dir(self.repo / "nested")
        git(self.repo / "nested", ["init", "-q"])
        put(self.repo / "nested" / "f.txt", "x\n")
        self.assertIn("nested", checkpoint.unavailable_reason(self.repo) or "")

    def test_a_checkpoint_on_a_detached_head_is_refused_and_keeps_the_work(self) -> None:
        git(self.repo, ["checkout", "-q", "--detach"])
        put(self.repo / "a.txt", "edited on no branch\n")
        before = git(self.repo, ["rev-parse", "HEAD"])
        with self.assertRaises(checkpoint.CheckpointError) as raised:
            checkpoint.checkpoint(self.repo)
        self.assertIn("detached", str(raised.exception))
        self.assertEqual(git(self.repo, ["rev-parse", "HEAD"]), before)
        self.assertEqual(read(self.repo / "a.txt"), "edited on no branch\n")

    def test_a_revert_leaves_edits_inside_a_submodule_alone_and_does_not_fail(self) -> None:
        """A checkpoint cannot commit what is inside a submodule, so a revert restores the repository around it and
        leaves the submodule's own files as they are, without raising after it has already changed the rest."""
        source = Path(self.outside.name)
        git(source, ["init", "-q", "-b", "main"])
        put(source / "s.txt", "inner\n")
        git(source, ["add", "-A"])
        git(source, ["commit", "-q", "-m", "inner"])
        git(self.repo, ["-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), "libs"])
        git(self.repo, ["commit", "-q", "-m", "add the submodule"])
        put(self.repo / "libs" / "s.txt", "changed inside\n")
        put(self.repo / "a.txt", "broken\n")
        put(self.repo / "agent_new.py", "x\n")
        checkpoint.revert(self.repo)
        self.assertEqual(read(self.repo / "a.txt"), "base\n")
        self.assertFalse(present(self.repo / "agent_new.py"))
        self.assertEqual(read(self.repo / "libs" / "s.txt"), "changed inside\n")

    def test_the_submodules_that_hold_changes_are_listed_by_path(self) -> None:
        source = Path(self.outside.name)
        git(source, ["init", "-q", "-b", "main"])
        put(source / "s.txt", "inner\n")
        git(source, ["add", "-A"])
        git(source, ["commit", "-q", "-m", "inner"])
        git(self.repo, ["-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), "libs"])
        git(self.repo, ["commit", "-q", "-m", "add the submodule"])
        self.assertEqual(checkpoint.dirty_submodules(self.repo), [])
        put(self.repo / "libs" / "new.txt", "inside\n")
        put(self.repo / "a.txt", "broken\n")
        self.assertEqual(checkpoint.dirty_submodules(self.repo), ["libs"])
        checkpoint.revert(self.repo)
        self.assertEqual(checkpoint.dirty_submodules(self.repo), ["libs"])

    def test_git_location_variables_in_the_environment_do_not_redirect_a_revert(self) -> None:
        """A GIT_DIR left in the environment, as git sets it for its own hooks, must not make a revert in one
        repository act on another."""
        other = Path(self.outside.name)
        git(other, ["init", "-q", "-b", "main"])
        put(other / "o.txt", "other\n")
        git(other, ["add", "-A"])
        git(other, ["commit", "-q", "-m", "other"])
        put(self.repo / "a.txt", "broken\n")
        self.addCleanup(restore_git, set_environment({"GIT_DIR": str(other / ".git")}))
        checkpoint.revert(self.repo)
        self.assertEqual(read(self.repo / "a.txt"), "base\n")
        self.assertEqual(read(other / "o.txt"), "other\n")

    def test_a_nested_repository_below_an_untracked_directory_is_not_committed(self) -> None:
        make_dir(self.repo / "x" / "y")
        git(self.repo / "x" / "y", ["init", "-q", "-b", "main"])
        put(self.repo / "x" / "y" / "f.txt", "1\n")
        git(self.repo / "x" / "y", ["add", "-A"])
        git(self.repo / "x" / "y", ["commit", "-q", "-m", "inner"])
        self.assertIn("nested", checkpoint.unavailable_reason(self.repo) or "")
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.checkpoint(self.repo)
        self.assertNotIn("160000", git(self.repo, ["ls-files", "--stage"]))

    def test_a_commit_message_hook_that_rewrites_the_subject_does_not_fail_the_checkpoint(self) -> None:
        install_hook(self.repo, "commit-msg", 'printf "[T-1] %s" "$(cat "$1")" > "$1"')
        put(self.repo / "b.txt", "x\n")
        put(self.repo / "trailing ", "y\n")
        sha = checkpoint.checkpoint(self.repo)
        self.assertEqual(sha, git(self.repo, ["rev-parse", "HEAD"]))
        self.assertTrue(git(self.repo, ["log", "-1", "--format=%s"]).startswith("[T-1] tcr: "))

    def test_an_unreadable_directory_stops_a_revert_before_it_changes_anything(self) -> None:
        put(self.repo / "a.txt", "dirty\n")
        make_dir(self.repo / "u")
        put(self.repo / "u" / "f.txt", "x\n")
        set_mode(self.repo / "u", 0)
        self.addCleanup(set_mode, self.repo / "u", 0o755)
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.revert(self.repo)
        self.assertEqual(read(self.repo / "a.txt"), "dirty\n")

    def test_commit_hooks_that_change_files_after_staging_are_reported_not_hidden(self) -> None:
        install_hook(self.repo, "pre-commit", "echo formatted >> a.txt")
        put(self.repo / "a.txt", "edited\n")
        before = git(self.repo, ["rev-parse", "HEAD"])
        with self.assertRaises(checkpoint.CheckpointError) as raised:
            checkpoint.checkpoint(self.repo)
        self.assertIn("hooks", str(raised.exception))
        self.assertNotEqual(git(self.repo, ["rev-parse", "HEAD"]), before)

    def test_a_tracked_file_name_that_is_not_utf8_does_not_break_the_checks(self) -> None:
        """Linux allows file names that are not valid UTF-8, and git lists them as the bytes they are. They must reach
        the caller as text it can print, not as a decoding error raised in the middle of a revert. The name goes into
        the index directly, since macOS cannot create such a file."""
        name = "bad\udcff"
        blob = git(self.repo, ["rev-parse", "HEAD:a.txt"])
        git(self.repo, ["update-index", "--add", "--cacheinfo", f"100644,{blob},{name}"])
        git(self.repo, ["commit", "-q", "-m", "a name that is not UTF-8"])
        self.assertIn(name, checkpoint.uncommitted(self.repo))
        self.assertIsInstance(checkpoint.unavailable_reason(self.repo), str)

    def test_a_directory_outside_a_git_tree_is_refused(self) -> None:
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.is_clean(Path(self.outside.name))


if __name__ == "__main__":
    unittest.main()
