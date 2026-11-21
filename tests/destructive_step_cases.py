"""Sources the scan of asserts after a discarding step must flag, which the review of fix10 to fix12 found it missed.

Copyright (c) 2026 benomahony. SPDX-License-Identifier: MIT
"""
from __future__ import annotations


def flagged_shapes_of_the_review() -> dict[str, str]:
    """The shapes of an assert after a discard that hid a crash and that the check missed: a raise of AssertionError, a branch, a finally, a returned tuple, a loop, a list a reading went into, and a discard by a rename, replace or move."""
    cases = {
        "an if that raises AssertionError": "def f(root):\n    names = uncommitted(root)\n    run_git(root, RESET_HARD)\n    if not names:\n        raise AssertionError('no names')\n",
        "an assert in a branch the discard never reaches": "def f(root, flag):\n    names = uncommitted(root)\n    if flag:\n        run_git(root, RESET_HARD)\n    else:\n        assert names\n",
        "an assert in the finally of a try that returns the discard": "def f(root):\n    names = uncommitted(root)\n    try:\n        return run_git(root, RESET_HARD)\n    finally:\n        assert names\n",
        "a callee called in the same returned tuple as the discard": "def g(names):\n    assert names\n\n\ndef f(root):\n    names = uncommitted(root)\n    return run_git(root, RESET_HARD), g(names)\n",
        "an assert earlier in a loop body than the discard": "def f(root, items):\n    for item in items:\n        names = uncommitted(root)\n        assert names\n        run_git(root, RESET_HARD)\n",
        "a reading appended to a list": "def f(root):\n    out = []\n    out.append(uncommitted(root))\n    run_git(root, RESET_HARD)\n    assert out\n",
        "a size read by os.path.getsize": "def f(path, root):\n    size = os.path.getsize(path)\n    run_git(root, RESET_HARD)\n    assert size\n",
        "an os.path read in the assert": "def f(path, root):\n    run_git(root, RESET_HARD)\n    assert os.path.isdir(path)\n",
        "an output read by subprocess.getoutput": "def f(root):\n    text = subprocess.getoutput('git status')\n    run_git(root, RESET_HARD)\n    assert text\n",
        "a read by shutil": "def f(path, root):\n    free = shutil.disk_usage(path)\n    run_git(root, RESET_HARD)\n    assert free\n",
        "a dataclass field set by position and asserted in the function": "@dataclass\nclass Facts:\n    reset: str\n    after: list\n\n\ndef f(root, reset):\n    names = uncommitted(root)\n    facts = Facts(reset, names)\n    run_git(root, RESET_HARD)\n    assert facts.after\n",
        "a dataclass field set by keyword and asserted in the function": "@dataclass\nclass Facts:\n    reset: str\n    after: list\n\n\ndef f(root, reset):\n    names = uncommitted(root)\n    facts = Facts(reset=reset, after=names)\n    run_git(root, RESET_HARD)\n    assert facts.after\n",
        "a dataclass field asserted by a method of the object": "@dataclass\nclass Facts:\n    reset: str\n    after: list\n\n    def check(self):\n        assert self.after\n\n\ndef f(root, reset):\n    names = uncommitted(root)\n    facts = Facts(reset, names)\n    run_git(root, RESET_HARD)\n    facts.check()\n",
        "a dataclass field asserted by a function the object is given to": "@dataclass\nclass Facts:\n    reset: str\n    after: list\n\n\ndef g(facts):\n    assert facts.after\n\n\ndef f(root, reset):\n    names = uncommitted(root)\n    facts = Facts(reset, names)\n    run_git(root, RESET_HARD)\n    g(facts)\n",
        "a discard by Path.rename": "def f(root, path):\n    names = uncommitted(root)\n    path.rename(root)\n    assert names\n",
        "a discard by os.replace": "def f(root, path):\n    names = uncommitted(root)\n    os.replace(path, root)\n    assert names\n",
        "a discard by shutil.move": "def f(root, path):\n    names = uncommitted(root)\n    shutil.move(path, root)\n    assert names\n",
    }
    return cases
