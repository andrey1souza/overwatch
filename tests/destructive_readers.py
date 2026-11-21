"""Which functions of the destructive path can fail or answer nothing, and are therefore readers whose answers no assert may check after work is discarded: found from the source of the modules, not listed by hand.

Copyright (c) 2026 benomahony. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import ast
import functools

from paths import ROOT

ENGINE = ROOT / "engine"
DESTRUCTIVE_PATH_MODULES = ("tcr", "tcr_texts", "checkpoint", "saved_revert", "checkpoint_git", "repository_state", "secrets_guard", "records", "messages", "safe_text")
METHOD_READERS = frozenset({"commit_of", "fingerprint", "workspace_files", "read_text", "read_bytes", "stat", "lstat", "is_dir", "is_file", "exists", "is_symlink", "iterdir", "readlink"})
ANY_FUNCTION = "*"
FALLIBLE_MODULE_READERS = frozenset({("subprocess", "run"), ("subprocess", "getoutput"), ("subprocess", "getstatusoutput"), ("os", "stat"), ("os", "lstat"), ("json", "load"), ("json", "loads"),
                                    ("os.path", ANY_FUNCTION), ("shutil", ANY_FUNCTION)})
FAILING_CALLS = frozenset({"run_git", "run", "check_call", "check_output", "open", "Popen", "read_text", "read_bytes", "stat", "lstat", "readlink", "iterdir"})
FAILURES_RAISED = frozenset({"CheckpointError", "GitExited", "RevertIncomplete", "Refused"})
STEP_OF_TCR = "answers the step of TCR that its caller returns whole, and no assert reads a part of it"
DISCARDER = "discards or saves the work, and what it answers, the saved ref or the new commit, it made before the discard or is not read after it"
NOT_READERS = {
    **dict.fromkeys(("after_edit", "checkpointed", "decide", "held", "judge", "reverted", "settle", "unsettled"), STEP_OF_TCR),
    **dict.fromkeys(("checkpoint", "revert", "save_discarded", "remove_untracked", "unstage", "point_ref_at", "snapshot_tree", "staged_copy", "identity_for_saved_commit"), DISCARDER),
}


def module_sources() -> dict[str, str]:
    """The source of each module of the destructive path."""
    return {module: (ENGINE / f"{module}.py").read_text() for module in DESTRUCTIVE_PATH_MODULES}


def failing_functions(sources: dict[str, str] | None = None) -> set[str]:
    """The functions of the destructive-path modules that can fail or answer nothing, by name: those whose body runs git or a process, opens or reads a file, or raises a failure of the checkpoint, and those that answer a value and call one of them, found by repeating until no name is added."""
    definitions = {function.name: function for source in (sources or module_sources()).values() for function in ast.parse(source).body if isinstance(function, ast.FunctionDef)}

    def fails_by_itself(function: ast.FunctionDef) -> bool:
        raises = {(node.exc.func if isinstance(node.exc, ast.Call) else node.exc) for node in ast.walk(function) if isinstance(node, ast.Raise) and node.exc is not None}
        return any(isinstance(node, ast.Call) and (called_name(node) in FAILING_CALLS or is_module_call(node, FALLIBLE_MODULE_READERS)) for node in ast.walk(function)) or any(isinstance(raised, ast.Name) and raised.id in FAILURES_RAISED for raised in raises)

    failing = {name for name, function in definitions.items() if fails_by_itself(function)}
    for _ in range(len(definitions) + 1):
        grown = failing | {name for name, function in definitions.items() if function.returns is not None and ast.unparse(function.returns) != "None"
                           and any(isinstance(node, ast.Call) and called_name(node) in failing for node in ast.walk(function))}
        if grown == failing:
            break
        failing = grown
    assert failing <= definitions.keys(), f"{failing - definitions.keys()} fail and are no function of the modules"
    return failing


@functools.cache
def fallible_readers() -> frozenset[str]:
    """The readers the scan follows: those that read a file or a value by a method, and every function of the destructive path that can fail, but those that discard work or answer a step."""
    readers = frozenset((METHOD_READERS | failing_functions()) - NOT_READERS.keys())
    assert not readers & NOT_READERS.keys(), f"{readers & NOT_READERS.keys()} are both readers and not"
    return readers


def called_name(call: ast.Call) -> str:
    """The name of the function a call makes: the attribute of a method call, the name of a plain call."""
    function = call.func
    return function.attr if isinstance(function, ast.Attribute) else function.id if isinstance(function, ast.Name) else ""


MAX_DOTS = 4


def dotted_name(node: ast.expr) -> str:
    """The dotted name that node writes, as os.path for the attribute path of os, or nothing when it is no chain of names, such as one that starts at a call; ValueError when a chain of names is deeper than MAX_DOTS."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    if len(parts) > MAX_DOTS:
        raise ValueError(f"the chain {node.id}.{'.'.join(reversed(parts))} is deeper than {MAX_DOTS} dots; raise MAX_DOTS")
    return ".".join([node.id, *reversed(parts)])


def is_module_call(call: ast.Call, pairs: frozenset[tuple[str, str]]) -> bool:
    """Whether call is module.function for one of the pairs, the module a dotted name as os.path, or a function of a module that the pairs name with ANY_FUNCTION."""
    function = call.func
    module = dotted_name(function.value) if isinstance(function, ast.Attribute) else ""
    return bool(module) and isinstance(function, ast.Attribute) and ((module, function.attr) in pairs or (module, ANY_FUNCTION) in pairs)
