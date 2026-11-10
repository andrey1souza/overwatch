"""Project policy for completion and explicitly protected tool actions."""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any

INHERITED = ("allow", "block")


class PolicyError(ValueError):
    """The gate in .overwatch.json is malformed; the message names the field and what to change."""


@dataclass(frozen=True)
class ProtectedAction:
    """One parsed gate.protected_actions entry: the tool it guards, an optional pattern its command must match, and
    the checks that guard it, where none means every check."""
    tool: str
    command: re.Pattern[str] | None
    checks: tuple[str, ...]


def parse_action(action: object) -> ProtectedAction:
    """Parses one gate.protected_actions entry, raising PolicyError that names the field to fix."""
    if not isinstance(action, dict):
        raise PolicyError(f"gate.protected_actions entry {action} must be an object with a tool; fix it in .overwatch.json")
    tool, checks, pattern = action.get("tool"), action.get("checks", []), action.get("command")
    if not isinstance(tool, str) or not tool:
        raise PolicyError(f"a protected action must name a tool, not {tool}; set its tool in .overwatch.json")
    if not isinstance(checks, list) or not all(isinstance(name, str) and name for name in checks):
        raise PolicyError(f"{tool}'s checks must be an array of check names, not {checks}; fix them in .overwatch.json")
    if pattern is not None and not isinstance(pattern, str):
        raise PolicyError(f"{tool}'s command must be a regular expression string, not {pattern}; fix it in .overwatch.json")
    try:
        command = None if pattern is None else re.compile(pattern)
    except re.error as error:
        raise PolicyError(f"{tool}'s command {pattern} is not a valid regular expression ({error}); fix it in .overwatch.json") from error
    parsed = ProtectedAction(tool, command, tuple(checks))
    assert parsed.tool == tool and len(parsed.checks) == len(checks), f"parsed {parsed} lost part of the {tool} action"
    assert (parsed.command is None) == (pattern is None), f"{tool}'s command {pattern} parsed to {parsed.command}"
    return parsed


@dataclass(frozen=True)
class Gate:
    """A project's parsed gate: whether inherited failures block, and the protected actions. Only read_gate builds
    one, after checking the whole gate object."""
    inherited: str
    actions: tuple[ProtectedAction, ...]


def read_gate(override: dict[str, Any]) -> Gate:
    """Parses the gate object of a project's .overwatch.json. Raises PolicyError naming the field to fix."""
    policy = override.get("gate", {})
    if not isinstance(policy, dict):
        raise PolicyError(f"gate must be an object, not {policy}; fix it in .overwatch.json")
    inherited, entries = policy.get("inherited", "allow"), policy.get("protected_actions", [])
    if inherited not in INHERITED:
        raise PolicyError(f"gate.inherited must be one of {INHERITED}, not {inherited}; fix it in .overwatch.json")
    if not isinstance(entries, list):
        raise PolicyError(f"gate.protected_actions must be an array, not {entries}; fix it in .overwatch.json")
    gate = Gate(inherited, tuple(parse_action(entry) for entry in entries))
    assert gate.inherited in INHERITED, f"gate was built with inherited {gate.inherited}; read_gate must check it first"
    assert all(type(action) is ProtectedAction for action in gate.actions), f"gate holds {gate.actions}; parse_action must build each one"
    return gate


def tool_command(arguments: object) -> str:
    """The command a tool call runs, from its command or cmd argument: a string as given, or an argument list quoted
    with shlex.join so each argument keeps its boundaries; any other value means no command."""
    command = arguments.get("command", arguments.get("cmd", "")) if isinstance(arguments, dict) else ""
    if isinstance(command, list) and all(isinstance(part, str) for part in command):
        text = shlex.join(command)
        assert shlex.split(text) == command, f"quoting {command} gave {text}, which splits differently; fix tool_command's quoting"
    else:
        text = command if isinstance(command, str) else ""
    assert text in ("", command) or isinstance(command, list), f"tool_command turned {command} into {text}; it may only quote lists"
    return text


COMMAND_BREAK = re.compile(r"&&|\|\||[;|\n]")
# Git subcommands that can bring files into the workspace from somewhere other than the agent's own edits.
FILE_BRINGING = frozenset({"am", "apply", "checkout", "cherry-pick", "merge", "pull", "read-tree", "rebase", "reset",
                           "restore", "revert", "stash", "switch"})
# Git options that take the next word as their value, so it is not the subcommand.
GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
# How many levels of quoting, as in sh -c 'sh -c "git merge x"', are read for a git command.
QUOTE_DEPTH = 3


def git_subcommand(words: list[str]) -> str | None:
    """The git subcommand among words: the first word after git that is no option and no option's value. None when
    words do not run git, or run it with no subcommand."""
    at = next((index for index, word in enumerate(words) if word.rsplit("/", 1)[-1] == "git"), None)
    found: str | None = None
    if at is not None:
        rest = iter(words[at + 1:])
        for word in rest:
            if word in GIT_VALUE_OPTIONS:
                next(rest, None)
            elif word and not word.startswith("-"):
                found = word
                break
    assert found is None or at is not None, f"found {found!r} in {words} with no git before it; look only after git"
    assert found is None or not found.startswith("-"), f"{found!r} is an option, not a subcommand; skip options"
    return found


def git_subcommands(command: str) -> list[str]:
    """The git subcommand of each part of command that runs git, in order: parts split at &&, ||, ;, | and new
    lines, global options such as -C dir skipped, and a quoted word holding a command, as in sh -c '...', read too."""
    names: list[str] = []
    texts = [command]
    for _ in range(QUOTE_DEPTH):
        nested: list[str] = []
        for text in texts:
            for part in COMMAND_BREAK.split(text):
                try:
                    words = shlex.split(part)
                except ValueError:
                    words = part.split()
                nested.extend(word for word in words if " " in word)
                name = git_subcommand(words)
                if name is not None:
                    names.append(name)
        texts = nested
    assert all(name != "" and not name.startswith("-") for name in names), f"subcommands {names} include an option or nothing; keep only words"
    assert len(names) <= len(command), f"{len(names)} subcommands from a command of {len(command)} characters; read each part once"
    return names


def brings_in_files(command: str) -> bool:
    """Whether command runs a git operation that can bring in files the agent did not write, as a merge, pull or
    cherry-pick can. Such a command may leave a plain commit in the reflog while the workspace holds another's changes."""
    names = git_subcommands(command)
    found = any(name in FILE_BRINGING for name in names)
    assert found == bool(FILE_BRINGING.intersection(names)), f"{names} gave {found}; test names against FILE_BRINGING"
    assert not found or names, f"{command!r} brings in files but names no git subcommand; read the subcommands first"
    return found


def protected_checks(tool: str, command: str, gate: Gate) -> list[str] | None:
    """The checks guarding a call of tool running command: None when no action protects it, an empty list when a
    protecting action names no checks and so means all of them. gate is a Gate, which only read_gate builds, so the
    project's policy has already been checked in full."""
    actions = gate.actions
    selected: set[str] = set()
    matched = [action for action in actions
               if action.tool.casefold() == tool.casefold() and (action.command is None or action.command.search(command))]
    for action in matched:
        if not action.checks:
            return []
        selected.update(action.checks)
    assert selected <= {name for action in actions for name in action.checks}, f"selected checks {selected} come from no action; fix protected_checks"
    assert bool(selected) == bool(matched), f"{len(matched)} matching actions selected {selected}; fix protected_checks"
    return sorted(selected) if matched else None
