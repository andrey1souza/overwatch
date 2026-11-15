"""Tests of what TCR mode does when a revert may lose work, a check has no earlier result, a start cannot be trusted or
the host's time runs out, driven through the Codex hook with real git repositories.

Copyright (c) 2026 benomahony. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from paths import ROOT
import checkpoint
import discovery
import engine
import records
import sessions
import tcr
import workspace
from test_checkpoint import git, isolate_git, make_dir, put, restore_git, set_environment, set_mode
from codex_hook import CodexHook, read, write
import test_tcr
from test_tcr import EDITING, FAILING, PASSING

FAILING_PYTEST = "=== 1 failed in 0.01s ==="
PASSING_PYTEST = "=== 1 passed in 0.01s ==="
FAILING_GO = "=== FAIL: . TestAdd (0.00s)\n\nDONE 2 tests, 1 failure in 0.184s"
PASSING_GO = "DONE 2 tests in 0.184s"
WORSE_GO = "=== FAIL: . TestAdd (0.00s)\n=== FAIL: . TestSub (0.00s)\n\nDONE 3 tests, 2 failures in 0.184s"
AWS_KEY = "AKIA" + "A" * 16
PROJECT_FILE = "[project]\n"
GO_MODULE = "module example\n"
SLOW_CHECK_SECONDS = 2
EXECUTABLE = 0o755


class HardeningTests(CodexHook):
    setUp = test_tcr.TcrHookTests.setUp
    set_setting = test_tcr.TcrHookTests.set_setting
    start = test_tcr.TcrHookTests.start
    subjects = test_tcr.TcrHookTests.subjects
    context = test_tcr.TcrHookTests.context

    def revert_refs(self) -> list[str]:
        refs = git(self.cwd, ["for-each-ref", "--format=%(refname)", "refs/overwatch/reverted"]).split()
        assert all(ref.startswith("refs/overwatch/reverted/") for ref in refs), f"a ref outside the saved ones was listed: {refs}"
        assert len(set(refs)) == len(refs), f"a saved ref was listed twice: {refs}"
        return refs

    def start_a_python_project(self, settings: dict[str, object] | None = None) -> None:
        """Starts a session on a committed project with one check, pytest, which passes, and the given settings."""
        write(self.cwd / ".overwatch.json", json.dumps({"mode": "tcr", **(settings or {})}))
        write(self.cwd / "pyproject.toml", PROJECT_FILE)
        self.tool("pytest", PASSING_PYTEST, 0)
        self.commit("start")
        started = self.call("SessionStart")[0]
        assert "TCR is on" in self.context(started), f"the session did not start TCR: {started}"
        assert self.subjects() == ["start"], f"the session started on {self.subjects()}, not one start commit"

    def edit(self, name: str) -> dict[str, object]:
        """The agent writes a new file and the hook reads the checks."""
        assert not (self.cwd / name).exists(), f"{name} is already in {self.cwd}; each edit writes a file of its own"
        write(self.cwd / name, "edit\n")
        output = self.call("PostToolUse", **EDITING)[0]
        assert isinstance(output, dict), f"the hook answered the edit of {name} with {output!r}, not an object"
        return output

    def add_go_check(self, output: str, code: int) -> dict[str, object]:
        """The agent's edit creates go.mod, so a check that was not there before runs, with the given result."""
        self.tool("gotestsum", output, code)
        assert not (self.cwd / "go.mod").exists(), f"go.mod is already in {self.cwd}; the check is not new"
        write(self.cwd / "go.mod", GO_MODULE)
        answer = self.call("PostToolUse", **EDITING)[0]
        assert isinstance(answer, dict), f"the hook answered the go.mod edit with {answer!r}, not an object"
        return answer

    def test_a_revert_saves_the_discarded_work_and_names_the_ref_by_default(self) -> None:
        result = self.start()
        write(result, FAILING, PASSING)
        write(self.cwd / "extra.txt", "new")
        output, _ = self.call("PostToolUse", **EDITING)
        [saved] = self.revert_refs()
        self.assertIn(saved, self.context(output))
        self.assertIn(f"git checkout {saved} -- .", self.context(output))
        self.assertEqual(git(self.cwd, ["show", f"{saved}:result.txt"]), FAILING)
        self.assertEqual(git(self.cwd, ["show", f"{saved}:extra.txt"]), "new")
        self.assertEqual(read(result), PASSING)
        self.assertFalse((self.cwd / "extra.txt").exists())
        self.assertEqual(git(self.cwd, ["status", "--porcelain"]), "")

    def test_a_revert_saves_nothing_and_says_the_work_is_gone_when_the_setting_is_false(self) -> None:
        result = self.start(settings={"tcr": {"saveReverted": False}})
        write(result, FAILING, PASSING)
        output, _ = self.call("PostToolUse", **EDITING)
        self.assertEqual(self.revert_refs(), [])
        self.assertIn("is gone", self.context(output))
        self.assertIn("saveReverted", self.context(output))
        self.assertEqual(read(result), PASSING)

    def test_an_allow_list_that_is_not_a_list_of_paths_names_the_field(self) -> None:
        self.start(settings={"tcr": {"allow": ".env"}})
        self.assertIn("tcr.allow", str(self.started["systemMessage"]))

    def test_a_green_edit_that_adds_a_secret_file_commits_nothing_and_tcr_is_unavailable(self) -> None:
        result = self.start()
        write(result, f"{PASSING} again", PASSING)
        write(self.cwd / ".env", "TOKEN=1\n")
        output, _ = self.call("PostToolUse", **EDITING)
        self.assertEqual(self.subjects(), ["start"])
        self.assertIn("TCR is unavailable", self.context(output))
        self.assertIn(".env (name pattern)", self.context(output))
        self.assertIn("tcr.allow", self.context(output))
        self.assertEqual(read(self.cwd / ".env"), "TOKEN=1\n")
        self.assertEqual(read(result), f"{PASSING} again")
        self.assertEqual(git(self.cwd, ["diff", "--cached", "--name-only"]), "")

    def test_a_secret_in_a_source_file_is_named_with_its_line(self) -> None:
        self.start()
        write(self.cwd / "src.py", f"one = 1\nkey = '{AWS_KEY}'\n")
        output, _ = self.call("PostToolUse", **EDITING)
        self.assertEqual(self.subjects(), ["start"])
        self.assertIn("src.py:2 (AWS access key)", self.context(output))

    def test_a_refused_checkpoint_does_not_revert_what_a_later_red_edit_leaves(self) -> None:
        result = self.start()
        write(self.cwd / ".env", "TOKEN=1\n")
        self.call("PostToolUse", **EDITING)
        write(result, FAILING, PASSING)
        self.call("PostToolUse", **EDITING)
        self.assertEqual(read(result), FAILING)
        self.assertEqual(read(self.cwd / ".env"), "TOKEN=1\n")

    def test_an_allowed_env_file_is_committed(self) -> None:
        self.start(settings={"tcr": {"allow": [".env"]}})
        write(self.cwd / ".env", "TOKEN=1\n")
        self.call("PostToolUse", **EDITING)
        self.assertEqual(self.subjects()[0], "tcr: .env")

    def test_a_generic_key_assignment_is_committed(self) -> None:
        self.start()
        write(self.cwd / "settings.py", 'api_key = "x"\n')
        self.call("PostToolUse", **EDITING)
        self.assertEqual(self.subjects()[0], "tcr: settings.py")

    def test_a_template_env_file_is_committed(self) -> None:
        self.start()
        write(self.cwd / ".env.example", "TOKEN=\n")
        self.call("PostToolUse", **EDITING)
        self.assertEqual(self.subjects()[0], "tcr: .env.example")

    def test_the_start_of_the_next_turn_after_a_revert_does_not_say_there_are_no_checks(self) -> None:
        result = self.start()
        write(result, FAILING, PASSING)
        self.call("PostToolUse", **EDITING)
        next_turn, _ = self.call("UserPromptSubmit")
        self.assertNotIn("TCR is unavailable", self.context(next_turn))
        write(result, FAILING, PASSING)
        again, _ = self.call("PostToolUse", **EDITING)
        self.assertIn("TCR reverted", self.context(again))

    def test_a_tcr_setting_that_is_not_an_object_names_the_field(self) -> None:
        self.start(settings={"tcr": "yes"})
        self.assertIn("'tcr'", str(self.started["systemMessage"]))

    def test_a_save_setting_that_is_not_a_boolean_names_the_field(self) -> None:
        self.start(settings={"tcr": {"saveReverted": "yes"}})
        self.assertIn("tcr.saveReverted", str(self.started["systemMessage"]))

    def test_an_unknown_tcr_setting_names_the_field(self) -> None:
        self.start(settings={"tcr": {"saveReverteds": True}})
        self.assertIn("saveReverteds", str(self.started["systemMessage"]))

    def test_a_failing_check_with_no_earlier_result_holds_the_edit_and_tells_the_agent(self) -> None:
        self.start_a_python_project()
        output = self.add_go_check(FAILING_GO, 1)
        self.assertEqual(self.subjects(), ["start"])
        self.assertEqual(read(self.cwd / "go.mod"), GO_MODULE)
        self.assertIn("TCR held the edit", self.context(output))
        self.assertIn("has no earlier result to compare with", self.context(output))
        self.assertIn("go failed", self.context(output))

    def test_a_held_edit_stays_held_while_the_new_check_fails_and_is_committed_when_it_passes(self) -> None:
        self.start_a_python_project()
        self.add_go_check(FAILING_GO, 1)
        again = self.edit("second.txt")
        self.assertEqual(self.subjects(), ["start"])
        self.assertIn("TCR held the edit", self.context(again))
        self.tool("gotestsum", PASSING_GO, 0, replacing=(FAILING_GO, 1))
        self.edit("third.txt")
        self.assertEqual(sorted(git(self.cwd, ["show", "--name-only", "--format=", "HEAD"]).split()), ["go.mod", "second.txt", "third.txt"])
        self.assertEqual(git(self.cwd, ["status", "--porcelain"]), "")

    def test_a_held_edit_survives_the_start_of_a_turn_and_the_next_passing_edit_commits_it(self) -> None:
        self.start_a_python_project()
        self.add_go_check(FAILING_GO, 1)
        next_turn, _ = self.call("UserPromptSubmit")
        self.assertNotIn("TCR is unavailable", self.context(next_turn))
        self.tool("gotestsum", PASSING_GO, 0, replacing=(FAILING_GO, 1))
        self.edit("second.txt")
        self.assertEqual(sorted(git(self.cwd, ["show", "--name-only", "--format=", "HEAD"]).split()), ["go.mod", "second.txt"])
        self.assertEqual(git(self.cwd, ["status", "--porcelain"]), "")

    def test_a_held_edit_does_not_survive_the_start_of_a_turn_when_a_person_changed_the_files(self) -> None:
        self.start_a_python_project()
        self.add_go_check(FAILING_GO, 1)
        write(self.cwd / "mine.txt", "mine\n")
        next_turn, _ = self.call("UserPromptSubmit")
        self.assertIn("TCR is unavailable", self.context(next_turn))
        self.tool("gotestsum", PASSING_GO, 0, replacing=(FAILING_GO, 1))
        self.edit("second.txt")
        self.assertEqual(self.subjects(), ["start"])

    def test_a_held_check_that_gets_worse_holds_the_edit_again_and_does_not_revert_it(self) -> None:
        self.start_a_python_project()
        self.add_go_check(FAILING_GO, 1)
        self.tool("gotestsum", WORSE_GO, 1, replacing=(FAILING_GO, 1))
        again = self.edit("second.txt")
        self.assertIn("TCR held the edit", self.context(again))
        self.assertNotIn("TCR reverted", self.context(again))
        self.assertEqual(read(self.cwd / "go.mod"), GO_MODULE)
        self.assertEqual(read(self.cwd / "second.txt"), "edit\n")
        self.assertEqual(self.subjects(), ["start"])

    def test_a_check_that_passed_after_being_held_reverts_a_later_failure(self) -> None:
        self.start_a_python_project()
        self.add_go_check(FAILING_GO, 1)
        self.tool("gotestsum", PASSING_GO, 0, replacing=(FAILING_GO, 1))
        self.edit("second.txt")
        self.tool("gotestsum", FAILING_GO, 1, replacing=(PASSING_GO, 0))
        reverted = self.edit("third.txt")
        self.assertFalse((self.cwd / "third.txt").exists())
        self.assertIn("TCR reverted", self.context(reverted))

    def test_a_known_regression_reverts_even_when_a_new_check_cannot_be_judged(self) -> None:
        self.start_a_python_project()
        self.tool("pytest", FAILING_PYTEST, 1, replacing=(PASSING_PYTEST, 0))
        reverted = self.add_go_check(FAILING_GO, 1)
        self.assertFalse((self.cwd / "go.mod").exists())
        self.assertEqual(self.subjects(), ["start"])
        self.assertIn("TCR reverted", self.context(reverted))

    def test_a_check_held_and_then_reverted_away_is_still_held_when_it_comes_back(self) -> None:
        self.start_a_python_project()
        self.tool("pytest", FAILING_PYTEST, 1, replacing=(PASSING_PYTEST, 0))
        self.add_go_check(FAILING_GO, 1)
        self.tool("pytest", PASSING_PYTEST, 0, replacing=(FAILING_PYTEST, 1))
        again = self.edit("go.mod")
        self.assertEqual(self.subjects(), ["start"])
        self.assertIn("TCR held the edit", self.context(again))

    def test_a_new_check_that_passes_does_not_hold_the_edit(self) -> None:
        self.start_a_python_project()
        self.add_go_check(PASSING_GO, 0)
        self.assertEqual(self.subjects()[0], "tcr: go.mod")

    def make_pytest_slow(self) -> None:
        """Replaces the passing pytest with one that takes SLOW_CHECK_SECONDS to pass."""
        script = self.bin / "pytest"
        put(script, f"#!/bin/sh\nsleep {SLOW_CHECK_SECONDS}\necho '{PASSING_PYTEST}'\n")
        set_mode(script, EXECUTABLE)
        assert read(script).count(PASSING_PYTEST) == 1, f"{script} does not pass after its sleep"
        assert os.access(script, os.X_OK), f"{script} cannot be run, so no check would pass"

    def test_an_edit_whose_checks_used_up_the_host_limit_is_left_alone_and_the_agent_is_told(self) -> None:
        self.start_a_python_project({"tcr": {"hostLimitSeconds": tcr.MIN_SECONDS_LEFT + 1}})
        self.make_pytest_slow()
        output = self.edit("slow.txt")
        self.assertEqual(self.subjects(), ["start"])
        self.assertTrue((self.cwd / "slow.txt").exists())
        self.assertIn("seconds the host gives a hook", self.context(output))

    def test_the_same_slow_checks_leave_time_enough_under_the_default_limit(self) -> None:
        self.start_a_python_project()
        self.make_pytest_slow()
        self.edit("slow.txt")
        self.assertEqual(self.subjects()[0], "tcr: slow.txt")

    def test_a_host_limit_that_is_not_a_positive_number_names_the_field(self) -> None:
        for value in (0, -3, "long", True):
            with self.subTest(value=value):
                put(self.cwd / ".overwatch.json", json.dumps({"mode": "tcr", "tcr": {"hostLimitSeconds": value}}))
                self.assertIn("tcr.hostLimitSeconds", str(self.call("SessionStart")[0]["systemMessage"]))

    def test_the_branch_main_is_named_when_tcr_starts(self) -> None:
        self.start(branch="main")
        self.assertIn("`main`", self.context(self.started))
        self.assertIn("checkpoints", self.context(self.started))

    def test_the_branch_master_is_named_when_tcr_starts(self) -> None:
        self.start(branch="master")
        self.assertIn("`master`", self.context(self.started))

    def test_another_branch_is_not_named(self) -> None:
        self.start(branch="feature")
        self.assertNotIn("checkpoints land on", self.context(self.started))
        self.assertIn("TCR is on", self.context(self.started))

    def test_files_that_a_check_wrote_are_named_in_the_reason_tcr_is_unavailable(self) -> None:
        result, _ = self.configure()
        runner = self.cwd / "check.py"
        text = read(runner)
        write(runner, text + "\nPath(__file__).parent.joinpath('artifact.log').write_text('x')\n", text)
        self.set_setting("mode", "tcr")
        write(result, PASSING)
        self.commit("start")
        started, _ = self.call("SessionStart")
        self.assertIn("TCR is unavailable", self.context(started))
        self.assertIn("artifact.log", self.context(started))
        self.assertIn("the checks changed the files while they ran", self.context(started))
        self.assertIn("add what a check writes to .gitignore", self.context(started))

    def call_here(self, event: str, **fields: str) -> dict[str, object]:
        """The hook's answer to one event, computed in this process so that a test can replace what the hook calls. The
        hook's process clock starts now, as it does for each real event."""
        tools = {"PATH": f"{self.bin}{os.pathsep}{os.defpath}", "PLUGIN_DATA": str(self.data)}
        sent = engine.Event(event, "session-1", str(self.cwd), fields.get("tool_name", ""), {}, "")
        with unittest.mock.patch.dict(os.environ, tools), unittest.mock.patch.object(tcr, "PROCESS_STARTED", time.monotonic()):
            output = engine.process_event(sent, engine.data_root(str(self.data)))
        assert isinstance(output, dict), f"the hook answered {event} with {output!r}, not an object"
        assert os.environ.get("PLUGIN_DATA") != str(self.data), f"the data directory {self.data} was left in the environment"
        return output

    def fail_pytest(self) -> None:
        """Swaps the passing pytest for one that fails."""
        self.tool("pytest", FAILING_PYTEST, 1, replacing=(PASSING_PYTEST, 0))
        assert FAILING_PYTEST in read(self.bin / "pytest"), f"{self.bin / 'pytest'} does not print the failure"
        assert PASSING_PYTEST not in read(self.bin / "pytest"), f"{self.bin / 'pytest'} still prints the pass"

    def test_a_revert_that_raises_makes_tcr_unavailable_with_the_error_and_leaves_the_files_and_commits(self) -> None:
        self.start_a_python_project()
        self.fail_pytest()
        write(self.cwd / "bad.txt", "edit\n")
        failing = unittest.mock.Mock(side_effect=checkpoint.CheckpointError("git took longer than 30 seconds"))
        with unittest.mock.patch.object(checkpoint, "revert", failing):
            output = self.call_here("PostToolUse", **EDITING)
        self.assertIn("TCR is unavailable: git took longer than 30 seconds", self.context(output))
        self.assertEqual(read(self.cwd / "bad.txt"), "edit\n")
        self.assertEqual(self.subjects(), ["start"])
        self.assertEqual(self.revert_refs(), [])
        again = self.call_here("PostToolUse", **EDITING)
        self.assertNotIn("TCR reverted", self.context(again))

    def test_a_revert_that_is_given_up_because_files_keep_changing_reverts_nothing_and_says_so(self) -> None:
        """The revert the hook asks for is given up after its saves, and the hook tells the agent so without reverting."""
        self.start_a_python_project()
        self.fail_pytest()
        write(self.cwd / "bad.txt", "edit\n")
        real_revert = checkpoint.revert
        written = [0]

        def write_another_file() -> None:
            written[0] += 1
            assert written[0] <= checkpoint.MAX_SAVE_ATTEMPTS, f"the revert saved {written[0]} times, more than {checkpoint.MAX_SAVE_ATTEMPTS}"
            write(self.cwd / f"flood{written[0]}.txt", "again\n")
            assert (self.cwd / f"flood{written[0]}.txt").exists(), f"flood{written[0]}.txt was not written to {self.cwd}"

        def revert_while_files_change(cwd: Path, *, save_ref: bool = True) -> str | None:
            assert save_ref, "the hook asked for a revert that does not save, so the files cannot keep changing while it saves"
            assert cwd.resolve() == self.cwd.resolve(), f"the hook reverted {cwd}, not {self.cwd}"
            return real_revert(cwd, save_ref=save_ref, after_save=write_another_file)

        with unittest.mock.patch.object(checkpoint, "revert", revert_while_files_change):
            output = self.call_here("PostToolUse", **EDITING)
        self.assertEqual(written[0], checkpoint.MAX_SAVE_ATTEMPTS)
        self.assertIn("TCR is unavailable", self.context(output))
        self.assertIn("kept changing", self.context(output))
        self.assertNotIn("TCR reverted", self.context(output))
        self.assertEqual(read(self.cwd / "bad.txt"), "edit\n")
        self.assertEqual(self.revert_refs(), [])

    def test_files_that_a_check_writes_during_a_turn_are_named_with_the_check_and_the_advice(self) -> None:
        self.start_a_python_project()
        put(self.bin / "pytest", f"#!/bin/sh\necho 'artifact' > artifact.txt\necho '{PASSING_PYTEST}'\n")
        set_mode(self.bin / "pytest", EXECUTABLE)
        output = self.edit("second.txt")
        self.assertIn("the check python wrote files (artifact.txt)", self.context(output))
        self.assertIn("add them to .gitignore", self.context(output))
        self.assertEqual(self.subjects(), ["start"])
        self.assertTrue((self.cwd / "second.txt").exists())

    def test_a_failing_check_when_no_file_differs_from_the_checkpoint_is_held_and_the_agent_is_told_there_is_nothing_to_revert(self) -> None:
        self.start_a_python_project()
        self.fail_pytest()
        text = read(self.cwd / "pyproject.toml")
        write(self.cwd / "pyproject.toml", text, text)
        output = self.call("PostToolUse", **EDITING)[0]
        self.assertIn("TCR left the latest change uncommitted and unreverted", self.context(output))
        self.assertIn("python reported === 1 failed", self.context(output))
        self.assertIn("no file differs from the latest checkpoint, so there is nothing to revert", self.context(output))
        self.assertEqual(self.subjects(), ["start"])

    def test_an_edit_held_while_another_check_is_held_resumes_at_the_next_turn_start(self) -> None:
        self.start_a_python_project()
        self.add_go_check(FAILING_GO, 1)
        self.tool("pytest", "error sending request for url", 1, replacing=(PASSING_PYTEST, 0))
        self.edit("second.txt")
        next_turn, _ = self.call("UserPromptSubmit")
        self.assertNotIn("TCR is unavailable", self.context(next_turn))
        self.tool("pytest", PASSING_PYTEST, 0, replacing=("error sending request for url", 1))
        self.tool("gotestsum", PASSING_GO, 0, replacing=(FAILING_GO, 1))
        self.edit("third.txt")
        self.assertEqual(sorted(git(self.cwd, ["show", "--name-only", "--format=", "HEAD"]).split()), ["go.mod", "second.txt", "third.txt"])

    def test_a_held_edit_whose_content_changed_with_the_same_size_and_time_does_not_resume(self) -> None:
        self.start_a_python_project()
        self.add_go_check(FAILING_GO, 1)
        held = self.cwd / "go.mod"
        stamp = held.stat()
        write(held, "module exampl3\n", GO_MODULE)
        os.utime(held, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertEqual((held.stat().st_size, held.stat().st_mtime_ns), (stamp.st_size, stamp.st_mtime_ns))
        next_turn, _ = self.call("UserPromptSubmit")
        self.assertIn("TCR is unavailable", self.context(next_turn))

    def test_a_revert_needs_the_time_for_its_git_calls_while_a_commit_needs_only_a_margin(self) -> None:
        limit = tcr.REVERT_SECONDS_LEFT - 1
        self.start_a_python_project({"tcr": {"hostLimitSeconds": limit}})
        self.fail_pytest()
        held = self.edit("bad.txt")
        self.assertIn("TCR held the edit", self.context(held))
        self.assertIn(f"reverting needs {tcr.REVERT_SECONDS_LEFT} more", self.context(held))
        self.assertEqual(read(self.cwd / "bad.txt"), "edit\n")
        self.assertEqual(self.revert_refs(), [])
        self.tool("pytest", PASSING_PYTEST, 0, replacing=(FAILING_PYTEST, 1))
        self.edit("good.txt")
        self.assertEqual(self.subjects()[0], "tcr: bad.txt, good.txt")
        self.assertEqual(git(self.cwd, ["status", "--porcelain"]), "")

    def test_a_revert_goes_ahead_when_the_limit_leaves_the_time_it_needs(self) -> None:
        self.start_a_python_project({"tcr": {"hostLimitSeconds": tcr.REVERT_SECONDS_LEFT + tcr.MIN_SECONDS_LEFT}})
        self.fail_pytest()
        output = self.edit("bad.txt")
        self.assertIn("TCR reverted", self.context(output))
        self.assertFalse((self.cwd / "bad.txt").exists())

    def test_a_revert_says_which_submodule_it_left_alone_instead_of_claiming_a_clean_tree(self) -> None:
        """A change inside a submodule that a revert cannot undo is named in the message, which does not say the tree is clean."""
        source = Path(self.temporary.name) / "inner"
        make_dir(source)
        git(source, ["init", "-q", "-b", "main"])
        put(source / "s.txt", "inner\n")
        git(source, ["add", "-A"])
        git(source, ["commit", "-q", "-m", "inner"])
        write(self.cwd / ".overwatch.json", json.dumps({"mode": "tcr"}))
        write(self.cwd / "pyproject.toml", PROJECT_FILE)
        self.tool("pytest", PASSING_PYTEST, 0)
        git(self.cwd, ["add", "-A"])
        git(self.cwd, ["-c", "user.name=test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "start"])
        git(self.cwd, ["-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), "libs"])
        self.commit("add the submodule")
        self.assertIn("TCR is on", self.context(self.call("SessionStart")[0]))
        self.fail_pytest()
        write(self.cwd / "libs" / "new.txt", "inside\n")
        write(self.cwd / "bad.txt", "edit\n")
        output = self.call("PostToolUse", **EDITING)[0]
        self.assertIn("TCR reverted", self.context(output))
        self.assertIn("Restored: `bad.txt`", self.context(output))
        self.assertIn("not restored", self.context(output))
        self.assertIn("`libs`", self.context(output))
        self.assertTrue((self.cwd / "libs" / "new.txt").exists())
    def make_pytest_hang(self) -> None:
        """Replaces the passing pytest with one that outlives its one-second deadline."""
        script = self.bin / "pytest"
        put(script, "#!/bin/sh\nexec sleep 30\n")
        set_mode(script, EXECUTABLE)
        assert "sleep 30" in read(script), f"{script} does not hang"
        assert os.access(script, os.X_OK), f"{script} cannot be run, so no check would hang"

    def test_a_check_that_outlives_its_deadline_holds_the_edit_and_reverts_nothing(self) -> None:
        self.start_a_python_project({"watchers": {"python": {"timeout": 1}}})
        self.make_pytest_hang()
        output = self.edit("slow.txt")
        self.assertEqual(self.subjects(), ["start"])
        self.assertEqual(read(self.cwd / "slow.txt"), "edit\n")
        self.assertIn("TCR held the edit", self.context(output))
        self.assertIn("outlived its deadline", self.context(output))
        self.assertNotIn("TCR reverted", self.context(output))

    def test_a_held_timeout_is_committed_with_the_edits_that_follow_once_the_check_passes(self) -> None:
        self.start_a_python_project({"watchers": {"python": {"timeout": 1}}})
        self.make_pytest_hang()
        self.edit("slow.txt")
        put(self.bin / "pytest", f"#!/bin/sh\necho '{PASSING_PYTEST}'\n")
        self.edit("second.txt")
        self.assertEqual(sorted(git(self.cwd, ["show", "--name-only", "--format=", "HEAD"]).split()), ["second.txt", "slow.txt"])
        self.assertEqual(git(self.cwd, ["status", "--porcelain"]), "")

    def test_a_new_failing_check_holds_the_edit_of_a_session_that_has_already_edited(self) -> None:
        self.start_a_python_project()
        self.edit("first.txt")
        self.assertEqual(self.subjects()[0], "tcr: first.txt")
        held = self.add_go_check(FAILING_GO, 1)
        self.assertIn("TCR held the edit", self.context(held))
        self.assertNotIn("TCR reverted", self.context(held))
        self.assertEqual(read(self.cwd / "go.mod"), GO_MODULE)
        again = self.edit("second.txt")
        self.assertIn("TCR held the edit", self.context(again))
        self.assertEqual(self.subjects()[0], "tcr: first.txt")

    def test_a_head_that_a_checkout_moved_clears_the_held_edit_so_the_next_turn_waits_for_a_clean_tree(self) -> None:
        self.start_a_python_project()
        self.add_go_check(FAILING_GO, 1)
        moved = git(self.cwd, ["commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "elsewhere"])
        git(self.cwd, ["checkout", "-q", "-b", "side", moved])
        next_turn, _ = self.call("UserPromptSubmit")
        self.assertIn("TCR is unavailable", self.context(next_turn))
        self.assertIn("uncommitted changes", self.context(next_turn))

    def saved_state(self) -> dict:
        root = sessions.data_root(str(self.data))
        sent = engine.Event("PostToolUse", "session-1", str(self.cwd), "Bash", {}, "")
        state = json.loads(sessions.state_path(sent, self.cwd.resolve(), root).read_text())["state"]
        assert "held" in state and "tcr" in state, f"the saved state {sorted(state)} has no held or tcr field"
        assert isinstance(state["held"], list), f"the held checks {state['held']!r} are no list"
        return state

    def test_a_pull_through_the_agents_shell_ends_the_hold_of_an_edit_the_person_committed(self) -> None:
        self.start_a_python_project()
        self.add_go_check(FAILING_GO, 1)
        self.assertTrue(self.saved_state()["held"])
        self.commit("the person commits")
        self.call("UserPromptSubmit")
        self.assertTrue(self.saved_state()["held"])
        moved = git(self.cwd, ["commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "a teammate's commit"])
        git(self.cwd, ["update-ref", "refs/heads/teammate", moved])
        git(self.cwd, ["merge", "-q", "--ff-only", "teammate"])
        output, _ = self.call("PostToolUse", tool_name="Bash", tool_input={"command": "git merge --ff-only teammate"})
        self.assertEqual(self.saved_state()["held"], [], f"the pull left the hold in place: {self.context(output)}")
        self.assertEqual(self.saved_state()["tcr"], "active")
        self.assertNotIn("TCR reverted", self.context(output))

    def test_an_edit_the_agent_made_while_the_first_checks_ran_is_named_as_the_reason_tcr_is_unavailable(self) -> None:
        write(self.cwd / ".overwatch.json", json.dumps({"mode": "tcr"}))
        write(self.cwd / "pyproject.toml", PROJECT_FILE)
        self.tool("pytest", PASSING_PYTEST, 0)
        self.commit("start")
        self.call("PreToolUse", **EDITING)
        write(self.cwd / "early.txt", "edit\n")
        started, _ = self.call("SessionStart")
        self.assertIn("TCR is unavailable", self.context(started))
        self.assertIn("the agent edited while the first checks ran", self.context(started))
        self.assertNotIn("a check wrote", self.context(started))
        self.assertEqual(self.subjects(), ["start"])

    def test_a_person_resetting_a_checkpoint_loses_nothing_to_a_later_failing_edit(self) -> None:
        self.start_a_python_project()
        self.edit("first.txt")
        git(self.cwd, ["reset", "-q", "--soft", "HEAD~1"])
        next_turn, _ = self.call("UserPromptSubmit")
        self.assertIn("TCR is unavailable", self.context(next_turn))
        self.tool("pytest", FAILING_PYTEST, 1, replacing=(PASSING_PYTEST, 0))
        output = self.edit("second.txt")
        self.assertNotIn("TCR reverted", self.context(output))
        self.assertEqual(read(self.cwd / "first.txt"), "edit\n")
        self.assertEqual(read(self.cwd / "second.txt"), "edit\n")
        self.assertEqual(self.subjects(), ["start"])


class HeldChecksStateTests(unittest.TestCase):
    def test_the_checks_held_for_want_of_an_earlier_result_are_saved_and_read_back(self) -> None:
        state = records.State(held=("python\0", "go\0sub"), tcr="active", held_fingerprint="0123abcd")
        self.assertEqual(records.state_from(json.loads(json.dumps(records.state_json(state)))), state)

    def test_a_state_saved_before_held_existed_holds_no_checks(self) -> None:
        saved = records.state_json(records.State())
        del saved["held"]
        self.assertEqual(records.state_from(saved).held, ())

    def test_held_checks_that_are_not_text_make_the_state_unreadable(self) -> None:
        saved = records.state_json(records.State())
        saved["held"] = [1]
        with self.assertRaises(records.StateError):
            records.state_from(saved)


class TimeBudgetTests(unittest.TestCase):
    def test_tcr_acts_only_with_time_left_for_git_before_the_host_stops_the_hook(self) -> None:
        last_good = tcr.HOST_LIMIT_SECONDS - tcr.MIN_SECONDS_LEFT
        limit = tcr.HOST_LIMIT_SECONDS
        needed = tcr.MIN_SECONDS_LEFT
        self.assertTrue(tcr.has_time_for_git(started=100.0, now=100.0 + last_good - 1, limit=limit, needed=needed))
        self.assertFalse(tcr.has_time_for_git(started=100.0, now=100.0 + last_good + 1, limit=limit, needed=needed))
        self.assertFalse(tcr.has_time_for_git(started=100.0, now=100.0 + limit + 1, limit=limit, needed=needed))

    def test_a_revert_needs_a_save_for_each_attempt_and_the_reset_and_the_clean_at_the_longest_git_takes(self) -> None:
        self.assertEqual(tcr.REVERT_SECONDS_LEFT, (checkpoint.MAX_SAVE_ATTEMPTS + 2) * checkpoint.GIT_TIMEOUT)
        revert_start = tcr.HOST_LIMIT_SECONDS - tcr.REVERT_SECONDS_LEFT
        self.assertTrue(tcr.has_time_for_git(100.0, 100.0 + revert_start - 1, tcr.HOST_LIMIT_SECONDS, tcr.REVERT_SECONDS_LEFT))
        self.assertFalse(tcr.has_time_for_git(100.0, 100.0 + revert_start + 1, tcr.HOST_LIMIT_SECONDS, tcr.REVERT_SECONDS_LEFT))

    def test_the_host_limit_is_the_shortest_one_a_host_gives_the_hook(self) -> None:
        hooks = json.loads(read(ROOT / "integrations" / "codex-hooks.json"))
        limits = [hook["timeout"] for groups in hooks["hooks"].values() for group in groups for hook in group["hooks"]]
        found = re.search(r"const TIMEOUT_MS = ([\d_]+)", read(ROOT / "engine" / "check.js"))
        self.assertIsNotNone(found)
        checker = int(found.group(1).replace("_", "")) // 1000
        self.assertEqual(tcr.HOST_LIMIT_SECONDS, min([*limits, checker]))
        self.assertGreaterEqual(tcr.MIN_SECONDS_LEFT, checkpoint.GIT_TIMEOUT)

    def setUp(self) -> None:
        self.addCleanup(restore_git, isolate_git())
        self.fake = tempfile.TemporaryDirectory()
        self.addCleanup(self.fake.cleanup)
        self.repo = tempfile.TemporaryDirectory()
        self.addCleanup(self.repo.cleanup)
        git(Path(self.repo.name), ["init", "-q"])
        assert (Path(self.repo.name) / ".git").is_dir(), f"{self.repo.name} is no repository to list the files of"
        assert Path(self.fake.name).is_dir() and not any(Path(self.fake.name).iterdir()), f"{self.fake.name} is not an empty directory for the fake git"

    def test_a_listing_that_takes_too_long_says_so(self) -> None:
        script = Path(self.fake.name) / "git"
        put(script, f'#!/bin/sh\n[ "$1" = ls-files ] && exec sleep {workspace.GIT_TIMEOUT * 4}\nexec {shutil.which("git")} "$@"\n')
        set_mode(script, EXECUTABLE)
        self.addCleanup(restore_git, set_environment({"PATH": f"{self.fake.name}{os.pathsep}{os.environ['PATH']}"}))
        with self.assertRaises(discovery.ConfigurationError) as caught:
            workspace.workspace_files(Path(self.repo.name))
        self.assertIn(f"took more than {workspace.GIT_TIMEOUT} seconds", str(caught.exception))
        self.assertIn("does nothing, TCR included", str(caught.exception))


class SettlingTests(unittest.TestCase):
    def test_an_edit_with_no_check_to_read_is_not_settled(self) -> None:
        current = tcr.State(fingerprint="files:digest", reports=())
        self.assertFalse(tcr.settles_edit(current, True))
        assert not current.reports, f"{current} holds reports; this test needs none"


if __name__ == "__main__":
    unittest.main()
