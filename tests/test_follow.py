"""engine.py follow tells a session of each settled result as a hook would, waking the agent only for its own edits."""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import textwrap
import unittest

import paths
from daemons import data_directory, eventually, is_running, stop_daemons
import engine

assert str(paths.ROOT / "engine") in sys.path, f"{paths.ROOT / 'engine'} is not on sys.path {sys.path}; paths puts engine/ there so engine imports by name"
ENGINE = paths.ROOT / "engine" / "engine.py"
# How long a test waits for follow to print the line it expects.
LINE_SECONDS = 20.0

# The fixture watcher: passes, then fails once the test writes the file it names, and keeps running.
WATCH = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    print("RESULT: PASS", flush=True)
    while not Path(sys.argv[1]).exists():
        time.sleep(0.05)
    print("RESULT: FAIL", flush=True)
    time.sleep(60)
    """
)


class FollowTests(unittest.TestCase):
    def setUp(self) -> None:
        """A git project whose only watcher is the fixture, a data directory for its daemon, and no follower yet."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = (Path(temporary.name) / "project").resolve()
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        self.broken = Path(temporary.name) / "broken"
        script = Path(temporary.name) / "watch.py"
        script.write_text(WATCH)
        self.data = data_directory(self)
        override = {"argv": [sys.executable, str(script), str(self.broken)], "summary": "^RESULT:.*$", "failed": "FAIL", "settle": 0.05,
                    "watchers": {"zanity": {"enabled": False}, "git": {"enabled": False}}}
        (self.project / ".overwatch.json").write_text(json.dumps(override))
        self.event = {"hook_event_name": "Follow", "session_id": "follow", "cwd": str(self.project), "integration": "claude"}
        assert (self.project / ".git").is_dir() and not self.broken.exists(), f"fixture project {self.project} is not ready"

    def start_follower(self) -> subprocess.Popen[str]:
        """engine.py follow for the session, given its event on stdin as a shim gives it, stopped when the test ends."""
        follower = subprocess.Popen([sys.executable, str(ENGINE), "follow"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    text=True, env={**os.environ, "PLUGIN_DATA": str(self.data), "PATH": "/usr/bin:/bin"})
        assert follower.stdin is not None and follower.stdout is not None, "follow started without its pipes"
        follower.stdin.write(json.dumps(self.event))
        follower.stdin.close()
        self.addCleanup(stop_daemons, self.data)
        self.addCleanup(follower.stdout.close)
        self.addCleanup(follower.wait)
        self.addCleanup(follower.kill)
        return follower

    def next_line(self, follower: subprocess.Popen[str]) -> dict[str, object]:
        """The next line of JSON follow prints, or a failure once LINE_SECONDS pass without one."""
        assert follower.stdout is not None, "follow has no output pipe"
        selector = selectors.DefaultSelector()
        selector.register(follower.stdout, selectors.EVENT_READ)
        try:
            ready = selector.select(LINE_SECONDS)
        finally:
            selector.close()
        self.assertTrue(ready, f"follow printed nothing within {LINE_SECONDS}s")
        return json.loads(follower.stdout.readline())

    def hook(self, name: str, tool: str = "") -> dict[str, object]:
        """Sends one hook event for the session to the engine in this process, as the session's shim does."""
        event = engine.Event(name, "follow", str(self.project), tool, {}, "claude")
        return engine.process_event(event, self.data)

    def settle_baseline(self) -> None:
        """Starts the session with its first, passing result as the baseline, as a session start does."""
        self.assertEqual(self.hook("SessionStart").get("overwatch", {}).get("guarded"), False)

    def test_a_failure_after_the_agents_edit_is_told_and_wakes_it(self) -> None:
        self.settle_baseline()
        follower = self.start_follower()
        self.hook("PreToolUse", "Edit")
        self.break_build()
        line = self.next_line(follower)
        self.assertIn("RESULT: FAIL", json.dumps(line))
        self.assertEqual((line["overwatch"]["guarded"], line["overwatch"]["waking"]), (True, True))

    def test_another_process_failure_is_told_without_waking(self) -> None:
        self.settle_baseline()
        follower = self.start_follower()
        self.break_build()
        line = self.next_line(follower)
        self.assertIn("RESULT: FAIL", json.dumps(line))
        self.assertEqual((line["overwatch"]["guarded"], line["overwatch"]["waking"]), (True, False))

    def start_tcr_session(self) -> None:
        """Commits the project in TCR mode and starts the session on that clean commit, with its passing baseline."""
        override = json.loads((self.project / ".overwatch.json").read_text())
        (self.project / ".overwatch.json").write_text(json.dumps({**override, "mode": "tcr"}))
        git = ["git", "-C", str(self.project), "-c", "user.name=test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]
        subprocess.run([*git, "add", "-A"], check=True, capture_output=True)
        subprocess.run([*git, "commit", "-q", "-m", "start"], check=True, capture_output=True)
        self.settle_baseline()
        assert json.loads((self.project / ".overwatch.json").read_text())["mode"] == "tcr", f"{self.project} does not run TCR"
        assert not subprocess.run(["git", "-C", str(self.project), "status", "--porcelain"], capture_output=True, text=True).stdout, f"{self.project} is not clean"

    def agent_writes(self, name: str) -> Path:
        """The agent's edit: a new file in the project, which the editing tool's PreToolUse has announced."""
        self.hook("PreToolUse", "Edit")
        edit = self.project / name
        edit.write_text("the agent's edit\n")
        assert edit.read_text() == "the agent's edit\n", f"{edit} does not hold the edit"
        assert edit.is_relative_to(self.project), f"{edit} is outside the project {self.project}"
        return edit

    def test_a_failure_the_follower_reads_is_never_judged_by_tcr(self) -> None:
        self.start_tcr_session()
        follower = self.start_follower()
        edit = self.agent_writes("edit.txt")
        self.break_build()
        line = self.next_line(follower)
        self.assertIn("RESULT: FAIL", json.dumps(line))
        self.assertNotIn("TCR reverted", json.dumps(line))
        self.assertTrue(edit.exists())

    def test_follow_stops_with_the_process_that_started_it(self) -> None:
        pid = self.orphaned_follower()
        self.assertFalse(eventually(lambda: is_running(pid), lambda running: not running, 2 * engine.PARENT_SECONDS + 2))

    def orphaned_follower(self) -> int:
        """The id of a follower whose parent, a shell, exited while it was following, a few seconds after it started."""
        # The shell stays a few seconds, so follow starts as its child and is following when the shell exits.
        script = f"echo '{json.dumps(self.event)}' | '{sys.executable}' '{ENGINE}' follow & echo $!; sleep 3"
        shell = subprocess.Popen(["sh", "-c", script], stdout=subprocess.PIPE, text=True,
                                 env={**os.environ, "PLUGIN_DATA": str(self.data), "PATH": "/usr/bin:/bin"})
        assert shell.stdout is not None, "the shell started without its output pipe"
        self.addCleanup(shell.stdout.close)
        self.addCleanup(stop_daemons, self.data)
        pid = int(shell.stdout.readline())
        shell.wait()
        assert is_running(pid), f"follow {pid} ended before its parent did, so its stopping says nothing"
        return pid

    def break_build(self) -> None:
        """Makes the fixture watcher print a failing build."""
        self.broken.touch()
        assert self.broken.exists(), f"{self.broken} was not made"


if __name__ == "__main__":
    unittest.main()
