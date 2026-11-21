"""A test that no assert in the destructive-path modules checks, after a step that discards work, a value that a reader which can fail gave: when the reader fails or answers nothing, the assert raises after the work is gone, and the agent is told nothing of the revert. An assert there checks what the function itself established, or a value it has just checked with a raise, or it sits in a try whose handler turns a bug into a told failure. The check follows a call made after the discarding step into the function it names, and the calls that function makes, three levels down, with the arguments that hold a reader's answer.

Copyright (c) 2026 benomahony. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import ast
import re
import unittest
from dataclasses import dataclass, field

from destructive_readers import (ENGINE, FALLIBLE_MODULE_READERS, MAX_DOTS, NOT_READERS, called_name, dotted_name, failing_functions, fallible_readers, is_module_call,
                                 module_sources)
from destructive_taint import ClassFields, dataclass_fields, Function, FunctionIndex, FUNCTIONS, given_objects, given_parameters, LiteralLists, Readings, readings_of, tainted_by
from destructive_step_cases import flagged_shapes_of_the_review

DISCARDING_CALLS = frozenset({"revert", "remove_untracked", "unlink", "rmtree", "rename"})
COMPOUND_VALUES = (ast.Tuple, ast.List, ast.Set, ast.Dict, ast.JoinedStr, ast.BinOp, ast.BoolOp)
DISCARDING_MODULE_CALLS = frozenset({("os", "remove"), ("os", "unlink"), ("os", "rmdir"), ("shutil", "rmtree"), ("os", "replace"), ("os", "rename"), ("shutil", "move")})
GIT_COMMAND_RUNNERS = frozenset({"run_git", "run", "check_call", "check_output"})
DISCARDING_GIT_WORDS = frozenset({"--hard", "clean", "checkout", "restore", "rm", "stash"})
KEEPS_THE_WORK_TREE = "--cached"
DISCARDING_GIT_CONSTANTS = frozenset({"RESET_HARD"})
RESET_TO_EARLIER_COMMIT = re.compile(r"HEAD[~^]")
DRY_RUN = re.compile(r"--dry-run|-[a-zA-Z]*n[a-zA-Z]*")
BUG_HANDLERS = frozenset({"ENGINE_BUGS", "UNEXPECTED_ERRORS", "AssertionError", "Exception"})
CALL_DEPTH = 3
NO_MORE_FILES = 99999
ADDED_TO_THE_REAL_CODE = {
    "a bound on the second listing in the revert": ("tcr", "        recovery = Recovery(error.saved, any(staged), skipped)\n",
                                                    f"        assert facts.after is None or len(facts.after) < {NO_MORE_FILES}\n"),
    "a check that each file of the second listing has a name": ("tcr_texts", "    if facts.after is None:\n        return unlisted_restoration(recovery, facts.bug)\n",
                                                                "    assert facts.after is None or all(facts.after)\n"),
    "a check on the second listing inside the object that holds it": ("tcr_texts", "        if self.after is None:\n            return [], [], []\n",
                                                                      "        assert self.after is None or not set(self.after) & set(self.before)\n"),
    "a size read by os.path.getsize after the reset": ("tcr", "        recovery = Recovery(error.saved, any(staged), skipped)\n",
                                                       "        assert os.path.getsize(root) < 99999\n"),
    "an output read by subprocess.getoutput after the reset": ("tcr", "        recovery = Recovery(error.saved, any(staged), skipped)\n",
                                                               "        assert subprocess.getoutput('git status')\n"),
}
ALLOWED: dict[tuple[str, str, str], str] = {
    ("checkpoint_git", "run_git", "result.returncode == 0"):
        "the git clean of remove_untracked runs in a loop, so the next round calls run_git after it; the assert checks the result of the command that call runs, a value it made a line above, and not a reading from before the discard",
    ("checkpoint_git", "run_git", "isinstance(stdout, str)"):
        "the same loop: the assert checks the output of the command that call runs, decoded a line above",
    ("tcr_texts", "restoration", "saved or not staged"):
        "staged is appended in the same step as `ref is not None and ...`, so a staged copy implies a saved ref by construction, whatever the readers answer",
    ("tcr_texts", "staged_copy_recovery", "bool(told) == staged"):
        "told is made from staged in the line above, so the equality holds for every value of staged",
    ("checkpoint", "revert", "saved is None or save_ref"):
        "saved is made by the conditional on save_ref before the reset, a parameter and not a reading, so nothing that fails after the reset changes it",
}


def is_dry_run(constants: list[str]) -> bool:
    """Whether the words of a git clean ask only what it would remove."""
    return "clean" in constants and any(DRY_RUN.fullmatch(word) for word in constants)


def words_of(call: ast.Call) -> tuple[list[str], set[str]]:
    """The strings and the names in the arguments of call."""
    words = [item for argument in call.args for item in ast.walk(argument)]
    return [item.value for item in words if isinstance(item, ast.Constant) and isinstance(item.value, str)], {item.id for item in words if isinstance(item, ast.Name)}


def literal_lists(tree: ast.AST) -> LiteralLists:
    """The strings of each name that a module or function binds to a literal list or tuple of strings, as RESET_HARD = ["reset", "--hard", "HEAD"]."""
    found: LiteralLists = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.List, ast.Tuple)):
            words = [item.value for item in node.value.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)]
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found.setdefault(target.id, []).extend(words)
    return found


def is_discarding(node: ast.AST, bound: LiteralLists | None = None, helpers: frozenset[str] = frozenset()) -> bool:
    """Whether node calls something that discards work: a revert, a removal, a function in helpers, or a git command that resets hard, cleans, checks out, restores, removes or stashes, run by run_git or subprocess, with its words given or held in a name that bound lists the strings of. A git clean that only lists what it would remove, and a git rm that keeps the work tree, discard nothing."""
    if not isinstance(node, ast.Call):
        return False
    if called_name(node) in DISCARDING_CALLS | helpers or is_module_call(node, DISCARDING_MODULE_CALLS):
        return True
    if called_name(node) not in GIT_COMMAND_RUNNERS:
        return False
    given, names = words_of(node)
    constants = [*given, *(word for name in names for word in (bound or {}).get(name, []))]
    resets = "reset" in constants and any(RESET_TO_EARLIER_COMMIT.match(word) for word in constants)
    discards = bool(DISCARDING_GIT_WORDS & set(constants)) or bool(DISCARDING_GIT_CONSTANTS & names) or resets
    return discards and not is_dry_run(constants) and KEEPS_THE_WORK_TREE not in constants


def discarding_functions(index: FunctionIndex, bound: LiteralLists) -> frozenset[str]:
    """The names of the functions that discard work, directly or through a function that does, found by repeating until no name is added."""
    helpers: frozenset[str] = frozenset()
    for _ in range(len(index) + 1):
        grown = helpers | {name for name, found in index.items() if any(is_discarding(node, bound, helpers) for _, function in found for node in ast.walk(function))}
        if grown == helpers:
            break
        helpers = grown
    assert helpers <= index.keys(), f"{helpers - index.keys()} discard and are no function of the index"
    return helpers


def catches_bugs(handler: ast.ExceptHandler) -> bool:
    """Whether the handler catches the engine's bugs, so that an assert that fails in its try is turned into a told failure."""
    return handler.type is not None and any((isinstance(item, ast.Name) and item.id in BUG_HANDLERS) or (isinstance(item, ast.Attribute) and item.attr in BUG_HANDLERS) for item in ast.walk(handler.type))


def tells_failure(handler: ast.ExceptHandler) -> bool:
    """Whether the handler turns what it caught into a told failure: it returns, calls something to report it, or raises another error, and it neither raises again what it caught nor does nothing."""
    nodes = [item for statement in handler.body for item in ast.walk(statement)]
    raises_again = any(isinstance(node, ast.Raise) and (node.exc is None or (isinstance(node.exc, ast.Name) and node.exc.id == handler.name)) for node in nodes)
    tells = any(isinstance(node, (ast.Return, ast.Call)) for node in nodes)
    return tells and not raises_again


def guarded_nodes(function: Function, kind: type[ast.AST] | tuple[type[ast.AST], ...]) -> set[int]:
    """The nodes of the kind, asserts or calls, in function that are in the try of a handler that catches bugs."""
    guarded: set[int] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Try) and any(catches_bugs(handler) and tells_failure(handler) for handler in node.handlers):
            guarded |= {id(item) for statement in node.body for item in ast.walk(statement) if isinstance(item, kind)}
    assert all(isinstance(item, int) for item in guarded), f"{guarded} hold something that is no {kind}"
    return guarded


def index_of(tree: ast.AST, module: str) -> FunctionIndex:
    """The functions of tree by name, async ones included."""
    index: FunctionIndex = {}
    for function in ast.walk(tree):
        if isinstance(function, FUNCTIONS):
            index.setdefault(function.name, []).append((module, function))
    assert all(index.values()), "a function name has no function"
    return index


@dataclass(frozen=True)
class Known:
    """What the scan knows of the whole destructive path, to follow a call into another module: its functions by name, the strings of its literal lists, and the fields of its dataclasses."""
    index: FunctionIndex
    bound: LiteralLists
    classes: ClassFields


@dataclass(frozen=True)
class Cutoff:
    """Where a discard ends in a function, and the nodes that run again after it because a loop holds the discard."""
    end: tuple[int, int]
    repeated: frozenset[int] = frozenset()

    def reaches(self, node: ast.AST) -> bool:
        """Whether node runs after the discard: it starts after it ends, or a loop holds both."""
        assert hasattr(node, "lineno"), f"{node!r} has no position, so it is no statement or expression"
        return (node.lineno, node.col_offset) > self.end or id(node) in self.repeated


EVERYTHING = Cutoff((0, 0))


def raises_an_assertion(node: ast.AST) -> bool:
    """Whether node is an if whose body raises AssertionError, which checks a condition as an assert does."""
    return isinstance(node, ast.If) and any(isinstance(item, ast.Raise) and isinstance(item.exc, ast.Call) and called_name(item.exc) == "AssertionError" for statement in node.body for item in ast.walk(statement))


def checks_of(function: Function) -> list[tuple[ast.AST, ast.expr]]:
    """The asserts of function and its ifs that raise AssertionError, each with the condition it checks."""
    return [(node, node.test) for node in ast.walk(function) if isinstance(node, ast.Assert) or raises_an_assertion(node)]


def asserts_on_readings(function: Function, module: str, readings: Readings, after: Cutoff) -> list[tuple[str, str, str]]:
    """The asserts of function that run after the cutoff and refer to a reader's answer and are not guarded."""
    guarded = guarded_nodes(function, (ast.Assert, ast.If))
    return [(module, function.name, ast.unparse(test)) for node, test in checks_of(function) if after.reaches(node) and id(node) not in guarded and tainted_by(test, readings)]


def called_functions(function: Function, readings: Readings, after: Cutoff, functions: FunctionIndex) -> list[tuple[str, Function, Readings]]:
    """The functions that function calls after the cutoff, outside a try that catches bugs, each with the parameters that the call gives a reader's answer as readings."""
    guarded_calls = guarded_nodes(function, ast.Call)
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call) and after.reaches(node) and id(node) not in guarded_calls]
    found = [(module, callee, readings_of(callee, given_parameters(call, callee, readings), given_objects(call, callee, readings), readings.classes)) for call in calls for module, callee in functions.get(called_name(call), []) if callee is not function]
    assert all(isinstance(callee, Function) for _, callee, _ in found), f"{found} include something that is no function"
    return found


def callee_hits(function: Function, readings: Readings, after: Cutoff, functions: FunctionIndex) -> list[tuple[str, str, str]]:
    """The asserts on a reader's answer in the functions that function calls after the cutoff, and in those they call, CALL_DEPTH levels down."""
    hits: list[tuple[str, str, str]] = []
    frontier = [("", function, readings, after)]
    for _ in range(CALL_DEPTH):
        reached = [(module, callee, callee_readings, EVERYTHING) for _, caller, caller_readings, below in frontier for module, callee, callee_readings in called_functions(caller, caller_readings, below, functions)]
        hits.extend(hit for module, callee, callee_readings, _ in reached for hit in asserts_on_readings(callee, module, callee_readings, EVERYTHING))
        frontier = reached
    assert all(len(hit) == 3 for hit in hits), f"{hits} include a hit that is no (module, function, condition)"
    return hits


def returns_with_no_later_step(function: Function) -> set[int]:
    """The nodes of the values that the function returns where nothing runs after the discard they hold: a value that is one call, outside a try with a finally, which runs after the return."""
    in_a_finally = {id(item) for node in ast.walk(function) if isinstance(node, ast.Try) and node.finalbody for statement in node.body for item in ast.walk(statement)}
    returns = [node for node in ast.walk(function) if isinstance(node, ast.Return) and node.value is not None and id(node) not in in_a_finally and not isinstance(node.value, COMPOUND_VALUES)]
    return {id(item) for node in returns for item in ast.walk(node.value)}


def cutoff_of(function: Function, discards: list[ast.Call]) -> Cutoff:
    """The first discard's end, and the nodes of each loop that holds a discard, which run after it in the next round."""
    first = min((node.end_lineno or 0, node.end_col_offset or 0) for node in discards)
    loops = [node for node in ast.walk(function) if isinstance(node, (ast.For, ast.AsyncFor, ast.While)) and any(id(call) in {id(item) for item in ast.walk(node)} for call in discards)]
    return Cutoff(first, frozenset(id(item) for loop in loops for item in ast.walk(loop)))


def asserts_on_readings_after_discarding(source: str, module: str, known: Known | None = None) -> list[tuple[str, str, str]]:
    """Each assert, or if that raises AssertionError, that runs after a discarding call and refers to a reader's answer, as (module, function, the condition): those of the function itself, in a branch or a later round of a loop included, and those of the functions that it calls after the call, CALL_DEPTH levels down, that refer to a reader's answer or to a parameter given one."""
    tree = ast.parse(source)
    own = Known(index_of(tree, module), literal_lists(tree), dataclass_fields(tree))
    functions, lists, fields = (known.index, known.bound, known.classes) if known is not None else (own.index, own.bound, own.classes)
    helpers = discarding_functions(functions, lists)
    hits: list[tuple[str, str, str]] = []
    for function in ast.walk(tree):
        if not isinstance(function, FUNCTIONS):
            continue
        readings = readings_of(function, classes=fields)
        returned = returns_with_no_later_step(function)
        discards = [node for node in ast.walk(function) if isinstance(node, ast.Call) and is_discarding(node, lists, helpers) and id(node) not in returned]
        if discards:
            cutoff = cutoff_of(function, discards)
            hits.extend(asserts_on_readings(function, module, readings, cutoff))
            hits.extend(callee_hits(function, readings, cutoff, functions))
    assert module, "the hits name no module"
    return list(dict.fromkeys(hits))


def asserts_of_the_destructive_path(sources: dict[str, str] | None = None) -> list[tuple[str, str, str]]:
    sources = sources or module_sources()
    index: FunctionIndex = {}
    for module, source in sources.items():
        for name, found in index_of(ast.parse(source), module).items():
            index.setdefault(name, []).extend(found)
    bound: LiteralLists = {}
    for source in sources.values():
        for name, words in literal_lists(ast.parse(source)).items():
            bound.setdefault(name, []).extend(words)
    classes = {name: names for source in sources.values() for name, names in dataclass_fields(ast.parse(source)).items()}
    known = Known(index, bound, classes)
    hits = [hit for module, source in sources.items() for hit in asserts_on_readings_after_discarding(source, module, known)]
    assert (ENGINE / "tcr.py").is_file(), f"{ENGINE} holds no tcr.py"
    return list(dict.fromkeys(hits))


def missed_cases() -> dict[str, str]:
    """Each shape of discard that the check does not find, which the README lists: it follows a function by the name it is called by, and a git command by the words it is given."""
    cases = {
        "a helper called under another name": "def wipe(root):\n    run_git(root, RESET_HARD)\n\n\ndef f(root):\n    names = uncommitted(root)\n    drop = wipe\n    drop(root)\n    assert names\n",
        "a discard by os.system": "def f(root):\n    names = uncommitted(root)\n    os.system('git reset --hard')\n    assert names\n",
        "a git command given as one string": "def f(root):\n    names = uncommitted(root)\n    run_git(root, 'reset --hard'.split())\n    assert names\n",
        "a discard by write_text": "def f(root, path):\n    names = uncommitted(root)\n    path.write_text('x')\n    assert names\n",
        "a discard by git update-ref -d": "def f(root):\n    names = uncommitted(root)\n    run_git(root, ['update-ref', '-d', 'refs/x'])\n    assert names\n",
        "a git command built from a parameter": "def f(root, arguments):\n    names = uncommitted(root)\n    run_git(root, arguments)\n    assert names\n",
    }
    assert all(cases.values()), "a case has no source"
    return cases


def flagged_cases() -> dict[str, str]:
    """Each shape of source with an assert after a discarding step on a reader's answer, by what it is."""
    cases = {
        "a field that a reading set, asserted through the step": "def f(turn):\n    head = commit_of(turn.cwd)\n    state = replace(turn, head=head)\n    checkpoint.revert(turn)\n    step = Step(state)\n    assert step.state.head is not None\n",
        "the reading itself": "def f(root):\n    names = uncommitted(root)\n    run_git(root, RESET_HARD)\n    assert names\n",
        "a value made from a reading": "def f(root):\n    names = uncommitted(root)\n    left = [name for name in names if name]\n    remove_untracked(root, names)\n    assert left\n",
        "a reading called in the assert": "def f(root):\n    run_git(root, ['clean', '-fd'])\n    assert has_commit(root)\n",
        "a subprocess result": "def f(root):\n    result = subprocess.run(['git'])\n    root.unlink()\n    assert result.returncode == 0\n",
        "a file read": "def f(root):\n    text = root.read_text()\n    shutil.rmtree(root)\n    assert text\n",
        "a json read": "def f(stream, root):\n    data = json.load(stream)\n    root.unlink()\n    assert data\n",
        "a walrus": "def f(root):\n    run_git(root, RESET_HARD)\n    if (names := uncommitted(root)):\n        pass\n    assert names\n",
        "the target of a for loop": "def f(root):\n    names = uncommitted(root)\n    run_git(root, RESET_HARD)\n    for name in names:\n        assert name\n",
        "an annotated assignment": "def f(root):\n    names: list[str] = uncommitted(root)\n    run_git(root, RESET_HARD)\n    assert names\n",
        "an augmented assignment": "def f(root):\n    left = []\n    left += uncommitted(root)\n    run_git(root, RESET_HARD)\n    assert left\n",
        "a reader under another name": "def f(root):\n    listing = uncommitted\n    names = listing(root)\n    run_git(root, RESET_HARD)\n    assert names\n",
        "the name of a with": "def f(root):\n    with has_commit(root) as flag:\n        pass\n    run_git(root, RESET_HARD)\n    assert flag\n",
        "a file removed by os.remove": "def f(root, path):\n    names = uncommitted(root)\n    os.remove(path)\n    assert names\n",
        "a reset to an earlier commit": "def f(root):\n    names = uncommitted(root)\n    run_git(root, ['reset', 'HEAD~1'])\n    assert names\n",
        "a hard reset run by subprocess": "def f(root):\n    names = uncommitted(root)\n    subprocess.run(['git', 'reset', '--hard'])\n    assert names\n",
        "a callee that asserts over the listing it is given": "def g(remaining):\n    assert len(set(remaining)) == len(remaining)\n\n\ndef f(root):\n    run_git(root, RESET_HARD)\n    remaining = uncommitted_now(root)\n    g(remaining)\n",
        "a callee that asserts over the root it is given": "def g(root):\n    assert root.is_dir()\n\n\ndef f(cwd):\n    root = repository_root(cwd)\n    run_git(root, RESET_HARD)\n    g(root)\n",
        "a callee that asserts over a reading of its own": "def g(root):\n    names = uncommitted(root)\n    assert names\n\n\ndef f(root):\n    run_git(root, RESET_HARD)\n    g(root)\n",
        "an assert in a try whose handler catches only a git failure": "def f(root):\n    run_git(root, RESET_HARD)\n    try:\n        names = uncommitted(root)\n        assert names\n    except OSError:\n        return None\n",
        "a callee two levels down": "def h(names):\n    assert names\n\n\ndef g(names):\n    return h(names)\n\n\ndef f(root):\n    names = uncommitted(root)\n    run_git(root, RESET_HARD)\n    g(names)\n",
        "an assert in a try whose handler raises again what it caught": "def f(root):\n    run_git(root, RESET_HARD)\n    try:\n        names = uncommitted(root)\n        assert names\n    except Exception:\n        raise\n",
        "an assert in a try whose handler raises the error it bound": "def f(root):\n    run_git(root, RESET_HARD)\n    try:\n        names = uncommitted(root)\n        assert names\n    except Exception as error:\n        raise error\n",
        "an assert in a try whose handler does nothing": "def f(root):\n    run_git(root, RESET_HARD)\n    try:\n        names = uncommitted(root)\n        assert names\n    except Exception:\n        pass\n",
        "a callee called in a try whose handler raises again": "def g(remaining):\n    assert remaining\n\n\ndef f(root):\n    names = uncommitted(root)\n    run_git(root, RESET_HARD)\n    try:\n        g(names)\n    except Exception:\n        raise\n",
        "a callee called in a try whose handler does nothing": "def g(remaining):\n    assert remaining\n\n\ndef f(root):\n    names = uncommitted(root)\n    run_git(root, RESET_HARD)\n    try:\n        g(names)\n    except Exception:\n        pass\n",
        "a discard inside a helper": "def wipe(root):\n    run_git(root, RESET_HARD)\n\n\ndef f(root):\n    names = uncommitted(root)\n    wipe(root)\n    assert names\n",
        "a discard inside a helper of a helper": "def wipe(root):\n    run_git(root, RESET_HARD)\n\n\ndef wipe_all(root):\n    wipe(root)\n\n\ndef f(root):\n    names = uncommitted(root)\n    wipe_all(root)\n    assert names\n",
        "a discard inside an async helper": "async def wipe(root):\n    run_git(root, RESET_HARD)\n\n\ndef f(root):\n    names = uncommitted(root)\n    wipe(root)\n    assert names\n",
        "an async function that discards": "async def f(root):\n    names = uncommitted(root)\n    await run_git(root, RESET_HARD)\n    assert names\n",
        "an async function that asserts after a helper": "def wipe(root):\n    run_git(root, RESET_HARD)\n\n\nasync def f(root):\n    names = uncommitted(root)\n    wipe(root)\n    assert names\n",
        "the reader committed_entry": "def f(root):\n    entry = committed_entry(root, 'a')\n    run_git(root, RESET_HARD)\n    assert entry\n",
        "a stat read": "def f(path, root):\n    size = path.stat()\n    run_git(root, RESET_HARD)\n    assert size\n",
        "an exists read in the assert": "def f(path, root):\n    run_git(root, RESET_HARD)\n    assert path.exists()\n",
        "an is_dir read in the assert": "def f(path, root):\n    run_git(root, RESET_HARD)\n    assert path.is_dir()\n",
        "a git restore": "def f(root):\n    names = uncommitted(root)\n    run_git(root, ['restore', '.'])\n    assert names\n",
        "a git rm": "def f(root):\n    names = uncommitted(root)\n    run_git(root, ['rm', '-f', 'a'])\n    assert names\n",
        "a git stash": "def f(root):\n    names = uncommitted(root)\n    run_git(root, ['stash', '-u'])\n    assert names\n",
        "a reset held in a variable": "def f(root):\n    names = uncommitted(root)\n    arguments = ['reset', '--hard']\n    run_git(root, arguments)\n    assert names\n",
        "a clean held in a module constant": "CLEAN = ['clean', '-fd']\n\n\ndef f(root):\n    names = uncommitted(root)\n    run_git(root, CLEAN)\n    assert names\n",
        "a callee three levels down": "def i(names):\n    assert names\n\n\ndef h(names):\n    return i(names)\n\n\ndef g(names):\n    return h(names)\n\n\ndef f(root):\n    names = uncommitted(root)\n    run_git(root, RESET_HARD)\n    g(names)\n",
        "a callee given the reading by keyword": "def g(*, remaining):\n    assert remaining\n\n\ndef f(root):\n    names = uncommitted(root)\n    run_git(root, RESET_HARD)\n    g(remaining=names)\n",
    }
    cases.update(flagged_shapes_of_the_review())
    for reader in ("uncommitted_now", "is_clean", "staged_changes", "off_branch", "head_ref", "current_branch", "submodules", "nested_repositories"):
        cases[f"the reader {reader}"] = f"def f(root):\n    answer = {reader}(root)\n    run_git(root, RESET_HARD)\n    assert answer\n"
    assert all(cases.values()), "a case has no source"
    return cases


def left_alone_cases() -> dict[str, str]:
    """Each shape of source that the check leaves alone, by what it is."""
    cases = {
        "an assert before the discarding step": "def f(root):\n    names = uncommitted(root)\n    assert names\n    run_git(root, RESET_HARD)\n",
        "a field the function set without a reading": "def f(turn):\n    head = commit_of(turn.cwd)\n    state = replace(turn, head=head, failed=False)\n    checkpoint.revert(turn)\n    step = Step(state)\n    assert not step.state.failed\n",
        "a function that discards nothing": "def f(root):\n    names = uncommitted(root)\n    assert names\n",
        "a git command that discards nothing": "def f(root):\n    names = uncommitted(root)\n    run_git(root, ['status'])\n    assert names\n",
        "a git clean that only lists": "def f(root):\n    names = uncommitted(root)\n    run_git(root, ['clean', '-nfd'])\n    assert names\n",
        "a git clean with --dry-run": "def f(root):\n    names = uncommitted(root)\n    run_git(root, ['clean', '--dry-run', '-d'])\n    assert names\n",
        "a reset that only unstages": "def f(root):\n    names = uncommitted(root)\n    run_git(root, ['reset', '-q'])\n    assert names\n",
        "a reset to HEAD": "def f(root):\n    names = uncommitted(root)\n    subprocess.run(['git', 'reset', 'HEAD'])\n    assert names\n",
        "a walrus of a plain value": "def f(root):\n    run_git(root, RESET_HARD)\n    if (size := len(root.name)):\n        pass\n    assert size\n",
        "a for loop over a plain value": "def f(root, items):\n    names = uncommitted(root)\n    run_git(root, RESET_HARD)\n    for item in items:\n        assert item\n",
        "a name given a function that reads nothing": "def f(root, items):\n    names = uncommitted(root)\n    run_git(root, RESET_HARD)\n    count = len\n    assert count(items)\n",
        "a with of a plain value": "def f(root, path):\n    run_git(root, RESET_HARD)\n    with path.open() as handle:\n        pass\n    assert handle\n",
        "a callee called before the discarding step": "def g(remaining):\n    assert remaining\n\n\ndef f(root):\n    names = uncommitted(root)\n    g(names)\n    run_git(root, RESET_HARD)\n",
        "a git rm that keeps the work tree": "def f(root):\n    names = uncommitted(root)\n    run_git(root, ['rm', '--cached', 'a'])\n    assert names\n",
        "a git command held in a variable that discards nothing": "def f(root):\n    names = uncommitted(root)\n    arguments = ['status']\n    run_git(root, arguments)\n    assert names\n",
        "a clean held in a variable that only lists": "def f(root):\n    names = uncommitted(root)\n    arguments = ['clean', '-nfd']\n    run_git(root, arguments)\n    assert names\n",
        "a helper that discards nothing": "def look(root):\n    run_git(root, ['status'])\n\n\ndef f(root):\n    names = uncommitted(root)\n    look(root)\n    assert names\n",
        "a helper that discards, returned from the function": "def wipe(root):\n    run_git(root, RESET_HARD)\n\n\ndef f(root, flag):\n    names = uncommitted(root)\n    if flag:\n        return wipe(root)\n    assert names\n",
        "a helper called before the assert it precedes only in another function": "def wipe(root):\n    run_git(root, RESET_HARD)\n\n\ndef g(root):\n    names = uncommitted(root)\n    assert names\n",
        "a dataclass field that no reading set": "@dataclass\nclass Facts:\n    reset: str\n    after: list\n\n    def check(self):\n        assert self.reset\n\n\ndef f(root, reset):\n    names = uncommitted(root)\n    facts = Facts(reset, names)\n    run_git(root, RESET_HARD)\n    facts.check()\n    assert facts.reset\n",
        "a dataclass made of plain values": "@dataclass\nclass Facts:\n    reset: str\n    after: list\n\n    def check(self):\n        assert self.after\n\n\ndef f(root, reset):\n    facts = Facts(reset, [])\n    run_git(root, RESET_HARD)\n    facts.check()\n",
        "a callee four levels down, which the check does not follow": "def j(names):\n    assert names\n\n\ndef i(names):\n    return j(names)\n\n\ndef h(names):\n    return i(names)\n\n\ndef g(names):\n    return h(names)\n\n\ndef f(root):\n    names = uncommitted(root)\n    run_git(root, RESET_HARD)\n    g(names)\n",
        "a callee given an object made of a reading, which is no reading": "def g(found):\n    assert found.saved\n\n\ndef f(root):\n    names = uncommitted(root)\n    run_git(root, RESET_HARD)\n    g(Found(names))\n",
        "a callee given a plain value": "def g(count):\n    assert count > 0\n\n\ndef f(root):\n    run_git(root, RESET_HARD)\n    g(3)\n",
        "an assert in a try whose handler turns a bug into a told failure": "def f(root):\n    run_git(root, RESET_HARD)\n    try:\n        names = uncommitted(root)\n        assert names\n    except (OSError, *ENGINE_BUGS):\n        return None\n",
        "an assert in a try whose handler reports the bug and returns": "def f(root):\n    run_git(root, RESET_HARD)\n    try:\n        names = uncommitted(root)\n        assert names\n    except Exception as error:\n        report_bug(error)\n        return None\n",
        "an assert in a try whose handler raises a told failure": "def f(root):\n    run_git(root, RESET_HARD)\n    try:\n        names = uncommitted(root)\n        assert names\n    except Exception as error:\n        raise Incomplete('failed') from error\n",
        "a callee called in a try whose handler turns a bug into a told failure": "def g(remaining):\n    assert remaining\n\n\ndef f(root):\n    names = uncommitted(root)\n    run_git(root, RESET_HARD)\n    try:\n        g(names)\n    except AssertionError:\n        return None\n",
    }
    assert all(cases.values()), "a case has no source"
    return cases


class AssertionsAfterDestructiveStepsTests(unittest.TestCase):
    def test_no_assert_after_a_discarding_step_checks_a_reading_except_the_ones_listed_with_their_reason(self) -> None:
        unlisted = [hit for hit in asserts_of_the_destructive_path() if hit not in ALLOWED]
        self.assertEqual(unlisted, [], f"{len(unlisted)} asserts follow a discarding step and check what a reader gave; remove them, or raise after checking the value before the step")

    def test_a_function_of_the_destructive_path_that_can_fail_is_a_reader_unless_it_is_listed_as_none_with_its_reason(self) -> None:
        failing = failing_functions()
        self.assertEqual(failing - fallible_readers() - NOT_READERS.keys(), set(), "a function that can fail is neither a reader nor listed as none")
        self.assertEqual(set(NOT_READERS) - failing, set(), "a function listed as no reader cannot fail now; remove it from the list")
        self.assertEqual([name for name, reason in NOT_READERS.items() if not reason], [], "a function listed as no reader has no reason")
        for name in ("off_branch", "head_ref", "uncommitted_now", "unavailable_reason", "run_git"):
            self.assertIn(name, fallible_readers(), f"{name} reads git and is not followed as a reader")

    def test_a_new_function_that_runs_git_or_reads_a_file_or_answers_what_such_a_function_gave_is_found_as_a_reader(self) -> None:
        found = failing_functions({"new": "def reads(root):\n    return run_git(root, ['status'])\n\n\ndef opens(path):\n    return open(path)\n\n\ndef raises(root):\n    raise CheckpointError('x')\n\n\n"
                                          "def relays(root) -> str:\n    return reads(root)\n\n\ndef plain(root) -> str:\n    return 'x'\n\n\ndef discards(root) -> None:\n    reads(root)\n"})
        self.assertEqual(found, {"reads", "opens", "raises", "relays"}, "the derivation misses a function that can fail or finds one that cannot")

    def test_each_assert_shape_the_review_found_is_flagged_when_it_is_added_back_to_the_real_code(self) -> None:
        for label, (module, anchor, added) in ADDED_TO_THE_REAL_CODE.items():
            with self.subTest(label):
                sources = module_sources()
                self.assertEqual(sources[module].count(anchor), 1, f"{label}: the anchor is not in {module} exactly once")
                sources[module] = sources[module].replace(anchor, anchor + added)
                flagged = [hit for hit in asserts_of_the_destructive_path(sources) if hit not in ALLOWED and hit not in asserts_of_the_destructive_path()]
                self.assertEqual(len(flagged), 1, f"the scan does not flag {label} in the real code")

    def test_every_allowed_assert_still_exists_and_has_a_reason(self) -> None:
        self.assertEqual([hit for hit in ALLOWED if hit not in set(asserts_of_the_destructive_path())], [], "an allowed assert is gone; remove it from the list")
        self.assertEqual([hit for hit, reason in ALLOWED.items() if not reason], [], "an allowed assert has no reason")

    def test_the_check_finds_each_shape_of_the_assert_that_crashed_a_revert_and_leaves_asserts_over_what_the_function_made_alone(self) -> None:
        """Each shape of an assert after a discarding step on a reader's answer is found, as the field it set, the answer, a value made from it, a reader called in the assert, and a process, file or JSON read; an assert before the step, on a field the function set without a reading, or in a function that discards nothing is not."""
        for label, source in flagged_cases().items():
            with self.subTest(label):
                self.assertEqual(len(asserts_on_readings_after_discarding(source, "case")), 1, f"the check misses {label}")
        for label, source in left_alone_cases().items():
            with self.subTest(label):
                self.assertEqual(asserts_on_readings_after_discarding(source, "case"), [], f"the check flags {label}")

    def test_the_check_flags_an_assert_in_a_branch_the_discarding_call_never_reaches(self) -> None:
        self.assertEqual(len(asserts_on_readings_after_discarding(flagged_cases()["an assert in a branch the discard never reaches"], "case")), 1, "the check does not flag an assert in the other branch")

    def test_a_new_function_that_fails_through_a_call_the_test_does_not_list_is_not_found_as_a_reader(self) -> None:
        self.assertEqual(failing_functions({"new": "def reads(url):\n    return urllib.request.urlopen(url)\n"}), set(), "the derivation finds a function that fails through a call it does not list, which the README says it does not")

    def test_the_check_does_not_find_the_discards_of_its_missed_cases(self) -> None:
        for label, source in missed_cases().items():
            with self.subTest(label):
                self.assertEqual(asserts_on_readings_after_discarding(source, "case"), [], f"the check finds {label}, which the README says it does not")


class DottedNameTests(unittest.TestCase):
    def test_a_chain_of_names_up_to_the_limit_is_read_and_a_deeper_one_fails_loudly(self) -> None:
        within = ".".join(["a"] * (MAX_DOTS + 1))
        self.assertEqual(dotted_name(ast.parse(within, mode="eval").body), within, "a chain within the limit was not read")
        beyond = ".".join(["a"] * (MAX_DOTS + 2))
        with self.assertRaises(ValueError, msg="a chain deeper than the limit was read as no name"):
            dotted_name(ast.parse(beyond, mode="eval").body)
        self.assertEqual(dotted_name(ast.parse("f().a.b", mode="eval").body), "", "a chain that starts at a call is no name")
        rooted_at_a_call = "f()" + ".a" * (MAX_DOTS + 1)
        self.assertEqual(dotted_name(ast.parse(rooted_at_a_call, mode="eval").body), "", "a chain deeper than the limit that starts at a call is no name, not an error")


if __name__ == "__main__":
    unittest.main()
