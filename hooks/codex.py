#!/usr/bin/env python3
"""One-shot check runner for Overwatch's coding-agent adapters.

Command hooks and in-process plugins call this runner with a Claude-style hook
event. It checks the git repository holding the event's directory, and nothing
outside one. It runs one build whenever the workspace changes and caches its
result for later hooks. A configured watch command is stopped after its first
complete report. The first result of each check is its baseline: only failures
it did not have are reported, each once, with the full output in a log file.

It runs on POSIX systems: it reads a watch command's output through selectors
on a pipe and holds each session's state under an flock.

Copyright (c) 2026 benomahony. SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from gating import protected_checks, read_gate, tool_command
from triage import InventoryError, parse_check, parse_inventory


OVERRIDE_PATH = ".overwatch.json"
COMMAND_TIMEOUT = 540
# How long a stopped command has to exit before it is killed, in seconds.
TERMINATE_GRACE = 3
# How long one read of a watch command's output waits for more, in seconds, and the most it reads at once, in bytes.
READ_WAIT = 0.1
READ_CHUNK = 65_536
# How long a git query may take, in seconds, before overwatch treats git as unavailable.
GIT_TIMEOUT = 5
# The directories a workspace fingerprint leaves out when git cannot list the workspace's files.
UNTRACKED = frozenset({
    ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".zig-cache", "zig-cache", "target", "dist", "build",
})
# The size, in bytes, of a workspace fingerprint's digest.
FINGERPRINT_BYTES = 20
FAILURE_LIMIT = 10
# Colour codes, which some tools (deno) print even into a pipe.
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# The decoration each tool puts around its summary, which the status line leaves out.
SUMMARY_DECORATION = re.compile(
    r"^Build Summary:\s*|^zanity:\s*|^=+\s*|\s*=+$|^Test Files\s+|^Test Suites:\s*|^DONE\s+|^(?:ok|FAILED) \| "
)

# The table every runtime reads; patterns are JavaScript regexes that Python reads the same way.
# Alternatives of one language are tried in order: the first whose marker, and tool if it needs one, are present runs.
TABLE: dict[str, Any] = json.loads((Path(__file__).resolve().parent.parent / "watchers.json").read_text())
WATCHERS: tuple[dict[str, Any], ...] = tuple(TABLE["watchers"])
CHECKERS: tuple[dict[str, Any], ...] = tuple(TABLE["checkers"])
# A language the project's root lacks is looked for in the directories this many levels down, leaving out hidden ones
# and these.
SEARCH_DEPTH: int = TABLE["search"]["depth"]
SEARCH_SKIP: frozenset[str] = frozenset(TABLE["search"]["skip"])
# Lines that mean a command could not run where it is, which no change to the code fixes.
ENVIRONMENT: tuple[re.Pattern[str], ...] = tuple(re.compile(pattern) for pattern in TABLE["environment"])


class ConfigurationError(Exception):
    """The project, its .overwatch.json or the hook event cannot be used; the message says what to change."""


class StateError(ValueError):
    """The saved state does not have the shape save_state writes, so the session starts afresh."""


@dataclass(frozen=True)
class Event:
    """One hook event as read_event checked it: the fields overwatch reads, each a string, and the tool's input."""
    hook_event_name: str
    session_id: str
    cwd: str
    tool_name: str
    tool_input: object


@dataclass(frozen=True)
class Report:
    """One check's result, as read_report reads it from the check's output."""
    name: str
    language: str
    command: str
    dir: str | None
    summary: str
    failed: bool
    failures: tuple[str, ...]
    environment: str | None
    text: str


@dataclass(frozen=True)
class First:
    """A check's first result in the session, its baseline: whether it failed, with what, and its summary."""
    failed: bool
    failures: tuple[str, ...]
    summary: str


@dataclass(frozen=True)
class Session:
    """The files of one session in one repository, all named once by session_at from the state file's path: the
    state, the lock held while it is read and saved, the triage inventory and the directory of full logs."""
    state: Path
    lock: Path
    inventory: Path
    logs: Path


@dataclass(frozen=True)
class State:
    """What one session in one repository knows between hook events. Saved by save_state, read back by state_from."""
    reports: tuple[Report, ...] = ()
    baseline: Mapping[str, First] = field(default_factory=dict)
    failed: bool = False
    told: str | None = None
    signatures: tuple[str, ...] = ()
    inherited: str = "allow"
    fingerprint: str | None = None
    environment: tuple[str, ...] = ()


EVENT_FIELDS = ("hook_event_name", "session_id", "cwd", "tool_name")
# What a StateError adds: what happens next, and what to do when it keeps happening.
REBUILT = "; the session starts afresh and reruns its checks, and if this repeats, delete the state files under PLUGIN_DATA"


def read_event() -> Event:
    """The hook event on stdin, or ConfigurationError naming the fields that are not strings."""
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as error:
        raise ConfigurationError(f"could not read the Codex hook event: {error}; send one JSON object on stdin") from error
    if not isinstance(value, dict):
        raise ConfigurationError(f"the Codex hook event was a JSON {type(value).__name__}; send one JSON object on stdin")
    fields = {name: value.get(name) or "" for name in EVENT_FIELDS}
    wrong = sorted(name for name, given in fields.items() if not isinstance(given, str))
    if wrong:
        raise ConfigurationError(f"the Codex hook event's fields {wrong} must be strings; send each as a JSON string")
    event = Event(**fields, tool_input=value.get("tool_input"))
    read = (event.hook_event_name, event.session_id, event.cwd, event.tool_name)
    assert read == tuple(fields[name] for name in EVENT_FIELDS), f"event {event} lost one of the fields {fields}"
    assert event.tool_input is value.get("tool_input"), f"event for {event.hook_event_name} replaced its tool input; pass it through"
    return event


def string_tuple(value: object, where: str) -> tuple[str, ...]:
    """value as a tuple of strings, or StateError naming where it was read when it is not a list of strings."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise StateError(f"{where} must be a list of strings, not {type(value).__name__}{REBUILT}")
    strings = tuple(value)
    assert len(strings) == len(value), f"{where} kept {len(strings)} of {len(value)} strings; copy every item with tuple()"
    assert list(strings) == value, f"{where} reordered its strings; keep them in order"
    return strings


def check_fields(value: dict[str, object], names: tuple[str, ...], optional: tuple[str, ...], where: str) -> None:
    """Raises StateError naming each field of value that is not what save_state writes: each of names a string, each
    of optional a string or null, failed a boolean, and no field outside names, optional, failed and failures."""
    unexpected = sorted(set(value) - {*names, *optional, "failed", "failures"})
    if unexpected:
        raise StateError(f"{where} has fields {unexpected} save_state never writes{REBUILT}")
    given = {name: value.get(name) for name in (*names, *optional)}
    wrong = [name for name, item in given.items() if not isinstance(item, str) and not (item is None and name in optional)]
    if not isinstance(value.get("failed"), bool):
        wrong.append("failed")
    if wrong:
        raise StateError(f"{where} has fields {wrong} of the wrong type{REBUILT}")
    assert all(isinstance(value[name], str) for name in names), f"{where} passed with a field of {names} that is not a string; add it to the type check above"
    assert isinstance(value["failed"], bool), f"{where} passed with failed {value['failed']}, not a boolean; check it before returning"


def report_from(value: object) -> Report:
    """A report as save_state wrote it, or StateError naming the fields that do not match."""
    if not isinstance(value, dict):
        raise StateError(f"a saved report is a JSON {type(value).__name__}, not an object{REBUILT}")
    check_fields(value, ("name", "language", "command", "summary", "text"), ("dir", "environment"), "a saved report")
    report = Report(name=value["name"], language=value["language"], command=value["command"], dir=value.get("dir"),
                    summary=value["summary"], failed=value["failed"],
                    failures=string_tuple(value.get("failures"), f"saved report {value['name']}'s failures"),
                    environment=value.get("environment"), text=value["text"])
    assert report.name == value["name"] and report.failed == value["failed"], f"saved report {value['name']} changed while read; copy each field as save_state wrote it"
    assert list(report.failures) == value["failures"], f"saved report {report.name} lost failures; keep each in order"
    return report


def first_from(key: str, value: object) -> First:
    """A check's saved first result, or StateError naming the check whose entry does not match."""
    if not isinstance(value, dict):
        raise StateError(f"the baseline of {key!r} is a JSON {type(value).__name__}, not an object{REBUILT}")
    check_fields(value, ("summary",), (), f"the baseline of {key!r}")
    first = First(value["failed"], string_tuple(value.get("failures"), f"the baseline of {key!r}'s failures"), value["summary"])
    assert first.summary == value["summary"], f"the baseline of {key!r} changed its summary while read; copy it as save_state wrote it"
    assert list(first.failures) == value["failures"], f"the baseline of {key!r} lost failures; keep each in order"
    return first


def state_from(value: object) -> State:
    """A state as save_state wrote it, or StateError naming the part that does not match."""
    if not isinstance(value, dict):
        raise StateError(f"the saved state is a JSON {type(value).__name__}, not an object{REBUILT}")
    reports, baseline, gate = value.get("reports", []), value.get("baseline", {}), value.get("gate", {})
    told, fingerprint = value.get("told"), value.get("fingerprint")
    if not isinstance(reports, list) or not isinstance(baseline, dict) or not isinstance(gate, dict):
        raise StateError(f"the saved state's reports, baseline or gate is not a list, object and object{REBUILT}")
    if not isinstance(value.get("failed", False), bool) or not all(item is None or isinstance(item, str) for item in (told, fingerprint)):
        raise StateError(f"the saved state's failed, told or fingerprint has the wrong type{REBUILT}")
    if gate.get("inherited", "allow") not in ("allow", "block"):
        raise StateError(f"the saved state's gate.inherited is {gate.get('inherited')}, not allow or block{REBUILT}")
    state = State(
        reports=tuple(report_from(report) for report in reports),
        baseline={key: first_from(key, first) for key, first in baseline.items()},
        failed=value.get("failed", False),
        told=told,
        signatures=string_tuple(value.get("signatures", []), "the saved state's signatures"),
        inherited=gate.get("inherited", "allow"),
        fingerprint=fingerprint,
        environment=string_tuple(value.get("environment", []), "the saved state's environment"),
    )
    assert len(state.reports) == len(reports) and set(state.baseline) == set(baseline), f"state kept {len(state.reports)} of {len(reports)} reports or lost baseline entries; read every saved entry"
    assert state.fingerprint == fingerprint, f"state changed its fingerprint {fingerprint} while read; copy it as saved"
    return state


def state_json(state: State) -> dict[str, object]:
    """state as save_state writes it, the shape state_from reads."""
    value: dict[str, object] = {
        "reports": [asdict(report) for report in state.reports],
        "baseline": {key: asdict(first) for key, first in state.baseline.items()},
        "failed": state.failed,
        "told": state.told,
        "signatures": list(state.signatures),
        "gate": {"inherited": state.inherited},
        "fingerprint": state.fingerprint,
        "environment": list(state.environment),
    }
    assert len(value["reports"]) == len(state.reports), f"state JSON holds {len(value['reports'])} of {len(state.reports)} reports; write every report"
    assert value["gate"] == {"inherited": state.inherited}, f"state JSON gate {value['gate']} does not hold {state.inherited}; write the gate as state_from reads it"
    return value


def read_override(cwd: Path) -> dict[str, Any]:
    path = cwd / OVERRIDE_PATH
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"could not read {path}: {error}") from error
    if not isinstance(value, dict):
        raise ConfigurationError(f"{path} must contain a JSON object")
    assert path.name == OVERRIDE_PATH, f"read {path} instead of {OVERRIDE_PATH}; build the path from cwd and OVERRIDE_PATH"
    assert all(isinstance(key, str) for key in value), f"{path} gave keys {list(value)}; read it with json.loads"
    return value


def string_list(value: Any, field: str) -> list[str] | None:
    """value as a list of strings, None when it is absent, or ConfigurationError naming field when it is neither."""
    assert field in ("argv", "baseline"), f"string_list was asked for field {field}; only argv and baseline are lists of strings"
    if value is None:
        return None
    strings = [item for item in value if isinstance(item, str)] if isinstance(value, list) else None
    if strings is None or len(strings) != len(value):
        raise ConfigurationError(f"{OVERRIDE_PATH} field {field!r} must be an array of strings")
    assert strings == value, f"{field} kept {len(strings)} of {len(value)} items; reject any item that is not a string"
    return strings


def apply_override(watcher: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(watcher)
    result["language"] = watcher.get("language", watcher["name"])
    for field in ("name", "summary", "failed"):
        if field in override:
            value = override[field]
            if not isinstance(value, str):
                raise ConfigurationError(f"{OVERRIDE_PATH} field {field!r} must be a string")
            result[field] = value
    for field in ("argv", "baseline"):
        if field in override:
            result[field] = string_list(override[field], field)
    try:
        for field in ("summary", "result", "failure"):
            if field in result:
                re.compile(result[field], re.MULTILINE)
        if "failed" in result:
            re.compile(result["failed"])
    except re.error as error:
        raise ConfigurationError(f"invalid regular expression in {OVERRIDE_PATH}: {error}") from error
    assert result["language"] == language_of(watcher), f"override changed {watcher['name']}'s language to {result['language']}; overrides may not set it"
    assert set(result) >= set(watcher), f"override dropped {set(watcher) - set(result)} from {watcher['name']}; copy every field of the watcher"
    return result


def language_of(watcher: dict[str, Any]) -> str:
    """The language a watcher checks: its language field, else its name. Watchers come from watchers.json, whose
    entries scripts/table.py checks, or through apply_override, which checks each field it takes from .overwatch.json;
    a language that is still not a non-empty string is reported as a configuration error."""
    language = watcher.get("language", watcher.get("name"))
    if not isinstance(language, str) or not language:
        raise ConfigurationError(f"watcher {watcher.get('name')!r} has language {language!r}; set a non-empty language or name in watchers.json")
    assert language in (watcher.get("language"), watcher.get("name")), f"watcher {watcher.get('name')} got language {language}, neither its language nor its name"
    assert "language" in watcher or language == watcher.get("name"), f"watcher {watcher.get('name')} got language {language}; fall back to its name"
    return language


def marker_in(directory: Path, marker: str) -> Path:
    """The path of marker inside directory. Markers come from watchers.json alone, and each must be a relative path
    with no parent steps, so a marker can only name a file within the directory it is looked for in."""
    relative = PurePosixPath(marker)
    if not marker or relative.is_absolute() or ".." in relative.parts:
        raise ConfigurationError(f"marker {marker!r} must be a relative path inside the project; fix it in watchers.json")
    path = directory.joinpath(*relative.parts)
    assert path.is_relative_to(directory), f"marker {marker} resolved to {path}, outside {directory}"
    assert path.name == relative.name, f"marker {marker} became {path}; join its parts in order"
    return path


def first_present(alternatives: list[dict[str, Any]], directory: Path) -> dict[str, Any] | None:
    """The first of a language's alternatives whose marker is in directory and whose tool, if it needs one, is on PATH."""
    assert len({language_of(watcher) for watcher in alternatives}) <= 1, f"alternatives span {[watcher['name'] for watcher in alternatives]}; pass one language's"
    present = next((watcher for watcher in alternatives
                    if marker_in(directory, watcher["marker"]).exists() and ("requires" not in watcher or shutil.which(watcher["requires"]))), None)
    assert present is None or directory.is_dir(), f"found {present['name'] if present else ''} in {directory}, which is not a directory"
    return present


def find_subprojects(cwd: Path) -> list[str]:
    """The directories SEARCH_DEPTH levels down, relative to cwd as POSIX paths, nearest and then alphabetically
    first. Links are not followed, so a link to a directory is not searched on any platform."""
    found: list[str] = []
    parents = [""]
    for _ in range(SEARCH_DEPTH):
        level: list[str] = []
        for parent in parents:
            try:
                entries = list(os.scandir(cwd.joinpath(*PurePosixPath(parent).parts)))
            except OSError:
                continue
            for entry in entries:
                if entry.is_dir(follow_symlinks=False) and not entry.name.startswith(".") and entry.name not in SEARCH_SKIP:
                    level.append(PurePosixPath(parent, entry.name).as_posix())
        level.sort()
        found.extend(level)
        parents = level
    assert len(set(found)) == len(found), f"subprojects {found} repeat a directory; visit each parent once"
    assert all(part.count("/") < SEARCH_DEPTH for part in found), f"subprojects {found} go past {SEARCH_DEPTH} levels; stop at SEARCH_DEPTH"
    return found


def find_project_watchers(cwd: Path, override: dict[str, Any]) -> list[dict[str, Any]]:
    # One watcher for each language the project has, or else for each sub-project that has it, one not inside
    # another that does; the override applies to the first. Sub-projects are POSIX paths relative to cwd, joined
    # onto it part by part, so they read the same on every platform.
    found: list[dict[str, Any]] = []
    subprojects: list[str] | None = None
    for language in dict.fromkeys(language_of(watcher) for watcher in WATCHERS):
        alternatives = [watcher for watcher in WATCHERS if language_of(watcher) == language]
        at_root = first_present(alternatives, cwd)
        if at_root is not None:
            found.append(apply_override(at_root, override if not found else {}))
            continue
        if subprojects is None:
            subprojects = find_subprojects(cwd)
        claimed: list[str] = []
        for directory in subprojects:
            if any(PurePosixPath(directory).is_relative_to(parent) for parent in claimed):
                continue
            watcher = first_present(alternatives, cwd.joinpath(*PurePosixPath(directory).parts))
            if watcher is None:
                continue
            claimed.append(directory)
            labelled = {**watcher, "language": language, "name": f"{watcher['name']} {directory}", "dir": directory}
            found.append(apply_override(labelled, override if not found else {}))
    keys = [key_of(watcher["name"], watcher.get("dir")) for watcher in found]
    assert len(set(keys)) == len(keys), f"watchers {keys} repeat a check; find each language once per directory"
    assert not any(Path(watcher.get("dir", "")).is_absolute() for watcher in found), f"watchers {keys} hold an absolute dir; keep dirs relative to {cwd}"
    return found


def find_watchers(cwd: Path) -> list[dict[str, Any]]:
    """The watchers and checkers to run in cwd, after its .overwatch.json override."""
    override = read_override(cwd)
    if override.get("enabled") is False:
        return []
    if "enabled" in override and not isinstance(override["enabled"], bool):
        raise ConfigurationError(f"{OVERRIDE_PATH} field 'enabled' must be a boolean")

    found = find_project_watchers(cwd, override)
    if not found:
        argv = string_list(override.get("argv"), "argv")
        baseline = string_list(override.get("baseline"), "baseline")
        complete = isinstance(override.get("summary"), str) and isinstance(override.get("failed"), str)
        if (argv is not None or baseline) and complete:
            found.append(apply_override(
                {"name": "project", "language": "project", "marker": OVERRIDE_PATH, "summary": r"$^", "failed": r"$^"},
                override,
            ))

    # A checker runs wherever its executable is on PATH, as shutil.which finds it there, with PATHEXT where the
    # platform has one.
    for checker in CHECKERS:
        if shutil.which(checker["marker"]):
            found.append(dict(checker))
    assert all("name" in watcher for watcher in found), f"found {found} with an unnamed watcher; every watcher needs a name"
    keys = [key_of(watcher["name"], watcher.get("dir")) for watcher in found]
    assert len(set(keys)) == len(found), f"found {keys} in {cwd}; find each check once"
    return found


def command_for(watcher: dict[str, Any]) -> tuple[list[str], bool] | None:
    """The command to run for a watcher's first result, and whether to stop it after its report."""
    baseline, argv = watcher.get("baseline"), watcher.get("argv")
    selected = (list(baseline), False) if baseline else (list(argv), True) if argv else None
    assert selected is None or selected[0], f"{watcher['name']} selected an empty command; return None when it has none"
    assert selected is None or selected[1] == (not baseline), f"{watcher['name']} would stop its baseline after a report; only stop argv"
    return selected


def command_line(argv: list[str]) -> str:
    """argv as one line, for messages and logs."""
    assert argv, "command_line was given an empty command; pass the argv a watcher runs"
    line = " ".join(argv)
    assert line.startswith(argv[0]), f"command line {line} does not start with {argv[0]}; join argv in order"
    return line


def terminate(process: subprocess.Popen[bytes]) -> bytes:
    """Stops process, killing it when it ignores the request, and returns what it printed meanwhile."""
    process.terminate()
    try:
        tail, _ = process.communicate(timeout=TERMINATE_GRACE)
    except subprocess.TimeoutExpired:
        process.kill()
        tail, _ = process.communicate()
    assert process.returncode is not None, f"process {process.pid} still runs after terminate; kill it"
    assert process.stdout is None or process.stdout.closed, f"process {process.pid}'s output is still open; communicate must read it to the end"
    return tail or b""


def read_available(selector: selectors.BaseSelector) -> bytes:
    """Whatever the registered output holds within READ_WAIT seconds, up to READ_CHUNK bytes; nothing when it holds none."""
    selected = selector.select(timeout=READ_WAIT)
    if not selected:
        return b""
    key, _ = selected[0]
    assert len(selected) == 1, f"selector watched {len(selected)} outputs; register only the process's stdout"
    chunk = os.read(key.fd, READ_CHUNK)
    assert len(chunk) <= READ_CHUNK, f"read {len(chunk)} bytes; read at most READ_CHUNK at once"
    return chunk


def run_command(
    cwd: Path, watcher: dict[str, Any], argv: list[str], stop_after_report: bool,
) -> tuple[str, int | None]:
    """Runs argv in cwd, returning what it printed and its exit code; None when stopped after its report. argv is
    a watcher's command from watchers.json or the project's own .overwatch.json, which belongs to whoever owns the
    project, as its Makefile does; it runs as an argument list, never through a shell, with no input."""
    if stop_after_report and "summary" not in watcher:
        raise ConfigurationError(f"{watcher['name']} runs a watch command but has no summary; give it one in .overwatch.json or watchers.json")
    assert argv, f"{watcher['name']} has an empty command; command_for must return None for it"
    command = command_line(argv)
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
    except OSError as error:
        raise ConfigurationError(f"could not start {command}: {error}") from error
    assert process.stdout is not None, f"{command} started without an output pipe; open it with stdout=PIPE"

    if not stop_after_report:
        try:
            output, _ = process.communicate(timeout=COMMAND_TIMEOUT)
        except subprocess.TimeoutExpired:
            terminate(process)
            raise ConfigurationError(f"{command} did not finish within {COMMAND_TIMEOUT}s")
        return output.decode(errors="replace"), process.returncode

    summary = re.compile(watcher["summary"], re.MULTILINE)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    chunks: list[bytes] = []
    deadline = time.monotonic() + COMMAND_TIMEOUT
    try:
        while time.monotonic() < deadline:
            chunk = read_available(selector)
            if chunk:
                chunks.append(chunk)
                text = ANSI.sub("", b"".join(chunks).decode(errors="replace"))
                if summary.search(text):
                    chunks.append(terminate(process))
                    return b"".join(chunks).decode(errors="replace"), None
            if process.poll() is not None:
                chunks.append(process.stdout.read() or b"")
                return b"".join(chunks).decode(errors="replace"), process.returncode
    finally:
        selector.close()
    terminate(process)
    raise ConfigurationError(f"{command} gave no report within {COMMAND_TIMEOUT}s")


def failures_in(watcher: dict[str, Any], output: str) -> list[str]:
    """Each failing test or error the output names, once each, in order."""
    if "failure" not in watcher:
        return []
    names: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(watcher["failure"], output, re.MULTILINE):
        name = next((group for group in match.groups() if group is not None), "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    assert len(names) == len(seen), f"failures {names} repeat a name; add each once"
    assert all(name == name.strip() and name for name in names), f"failures {names} keep blank or padded names; strip them and skip empty ones"
    return names


def environment_in(output: str) -> str | None:
    """The first line saying the command failed for want of something in its environment, not because of the code."""
    line = next((line.strip() for line in output.splitlines() if any(pattern.search(line) for pattern in ENVIRONMENT)), None)
    assert line is None or "\n" not in line, f"environment line {line} spans lines; take one line of the output"
    assert line is None or line in output, f"environment line {line} is not in the output; return the line as printed"
    return line


def read_report(watcher: dict[str, Any], output: str, code: int | None = None) -> Report | None:
    """Reads one build's output; a run with no summary line is read by its exit code. The watcher's patterns are
    strings that apply_override or scripts/table.py checked compile, and its name a non-empty string."""
    clean = ANSI.sub("", output)
    failures = failures_in(watcher, clean)
    match = re.search(watcher["summary"], clean, re.MULTILINE) if "summary" in watcher else None
    if match is not None:
        line = match.group(0).strip()
        shown = re.search(watcher["result"], clean, re.MULTILINE) if "result" in watcher else None
        summary = shown.group(0).strip() if shown else line
        failed = "failed" in watcher and re.search(watcher["failed"], line) is not None
    elif code is None or (code == 0 and "summary" in watcher):
        return None
    else:
        failed = code != 0
        summary = (f"{len(failures)} failed" if failures else f"exited with code {code}") if failed else "passed"
    selected = command_for(watcher)
    assert selected is not None, f"{watcher['name']} reported without a command; give it argv or baseline in watchers.json"
    report = Report(
        name=watcher["name"],
        language=language_of(watcher),
        command=command_line(selected[0]),
        dir=watcher.get("dir"),
        summary=summary,
        failed=failed,
        failures=tuple(failures) if failed else (),
        environment=environment_in(clean) if failed else None,
        text=clean.strip(),
    )
    assert report.failed or not report.failures, f"{watcher['name']} passed with failures {failures}; list failures only when it failed"
    return report


def run_check(cwd: Path, watcher: dict[str, Any]) -> Report | None:
    """Runs one watcher's command and reads its report, or None when it has no command."""
    selected = command_for(watcher)
    if selected is None:
        return None
    argv, stop_after_report = selected
    output, code = run_command(cwd / watcher.get("dir", ""), watcher, argv, stop_after_report)
    report = read_report(watcher, output, code)
    if report is None:
        tail = output.strip()[-2_000:]
        detail = f"\n\n{tail}" if tail else ""
        raise ConfigurationError(f"{command_line(argv)} gave no matching summary; make the summary pattern for {watcher['name']} in .overwatch.json match its output{detail}")
    assert report.name == watcher["name"], f"{watcher['name']} reported as {report.name}; name the report after its watcher"
    assert report.command == command_line(argv), f"{watcher['name']} reported {report.command} but ran {command_line(argv)}"
    return report


def run_checks(cwd: Path, watchers: list[dict[str, Any]]) -> list[Report]:
    """Runs every watcher's check at once, so a polyglot project waits for its slowest suite, not the sum of them."""
    if not watchers:
        return []
    with ThreadPoolExecutor(max_workers=len(watchers)) as pool:
        reports = list(pool.map(lambda watcher: run_check(cwd, watcher), watchers))
    assert len(reports) == len(watchers), f"ran {len(reports)} of {len(watchers)} checks; map every watcher"
    ran = [report for report in reports if report is not None]
    assert [report.name for report in ran] == [watcher["name"] for watcher, report in zip(watchers, reports) if report], f"reports {[report.name for report in ran]} are out of watcher order"
    return ran


def workspace_files(cwd: Path) -> list[Path]:
    """Every file git would track in cwd. Where git cannot list them, which is no error, since a workspace need not
    be one git can read, every file under cwd outside the UNTRACKED directories instead."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard", "-z"],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT,
            check=True,
        )
        paths = [cwd / os.fsdecode(part) for part in result.stdout.split(b"\0") if part]
    except (OSError, subprocess.SubprocessError):
        paths = []
        for root, directories, files in os.walk(cwd):
            directories[:] = [name for name in directories if name not in UNTRACKED]
            paths.extend(Path(root) / name for name in files)
    assert all(path.is_relative_to(cwd) for path in paths), f"listed {[path for path in paths if not path.is_relative_to(cwd)]} outside {cwd}; list only the workspace"
    assert len(set(paths)) == len(paths), f"listed {len(paths) - len(set(paths))} files of {cwd} twice; list each once"
    return paths


def fingerprint(cwd: Path) -> str:
    """A digest of the path, size and modification time of each of workspace_files(cwd). A file that goes missing
    while it is read is digested as missing, which is itself a change the next fingerprint sees."""
    digest = hashlib.blake2b(digest_size=FINGERPRINT_BYTES)
    paths = workspace_files(cwd)
    for path in sorted(paths):
        digest.update(os.fsencode(os.path.relpath(path, cwd)))
        try:
            stat = path.stat()
        except OSError:
            digest.update(b"\0missing")
        else:
            digest.update(f"\0{stat.st_size}:{stat.st_mtime_ns}".encode())
    value = digest.hexdigest()
    assert len(value) == FINGERPRINT_BYTES * 2, f"fingerprint {value} is not {FINGERPRINT_BYTES} bytes; keep digest_size=FINGERPRINT_BYTES"
    assert all(character in "0123456789abcdef" for character in value), f"fingerprint {value} is not hexadecimal; use hexdigest"
    return value


def repository_of(cwd: Path) -> Path | None:
    """The root of the git repository holding cwd; None outside one."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=GIT_TIMEOUT,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    root = Path(result.stdout.strip()).resolve()
    assert root.is_absolute(), f"repository root {root} is relative; resolve it"
    assert cwd.resolve().is_relative_to(root), f"repository root {root} does not hold {cwd}; ask git from cwd"
    return root


def data_root() -> Path:
    """The directory overwatch keeps session state in, resolved: PLUGIN_DATA, which the agent host that runs this
    hook sets to the plugin's own data directory, else ~/.cache/overwatch. A PLUGIN_DATA that is not an absolute
    path is refused, so state never lands relative to whatever directory the hook was started in."""
    given = os.environ.get("PLUGIN_DATA")
    if given is not None and not Path(given).is_absolute():
        raise ConfigurationError(f"PLUGIN_DATA is {given!r}, not an absolute path; set it to the plugin's data directory")
    root = Path(given or Path.home() / ".cache" / "overwatch").resolve()
    assert root.is_absolute(), f"data root {root} is relative; resolve it"
    assert root == root.resolve(), f"data root {root} still holds a link or a parent step; resolve it"
    return root


def in_data(path: Path) -> Path:
    """path resolved, or ConfigurationError when it lies outside data_root(), the only place overwatch reads state from."""
    root, resolved = data_root(), path.resolve()
    if not resolved.is_relative_to(root):
        raise ConfigurationError(f"state file {path} is outside {root}; set PLUGIN_DATA to the directory that holds it")
    assert resolved.is_absolute() and ".." not in resolved.parts, f"state file {resolved} is not a plain absolute path"
    assert resolved.is_relative_to(root), f"state file {resolved} escaped {root}"
    return resolved


def state_path(event: Event, cwd: Path) -> Path:
    """Where the state of one session in one repository is kept: named by a digest of both, so neither shapes the path."""
    data = data_root()
    identity = f"{event.session_id}\0{cwd}"
    path = data / f"{hashlib.sha256(identity.encode()).hexdigest()}.json"
    assert path.parent == data, f"state path {path} is outside {data}; name it by digest alone"
    assert path.suffix == ".json", f"state path {path} is not JSON; save_state's temporary file swaps the suffix"
    return path


def session_at(path: Path) -> Session:
    """The files of the session whose state is kept at path: the lock, the inventory and the log directory beside it."""
    session = Session(state=path, lock=path.with_suffix(".lock"), inventory=path.with_suffix(".baseline.json"), logs=path.with_suffix(""))
    assert len({session.state, session.lock, session.inventory, session.logs}) == 4, f"session files of {path} share a name; give each its own suffix"
    assert all(each.parent == path.parent for each in (session.lock, session.inventory, session.logs)), f"session files of {path} are not beside it"
    return session


def load_state(session: Session) -> State:
    """The state saved in session, or an empty one when none was saved yet. A file save_state did not write, being
    unreadable JSON or of another shape, is replaced: the session starts afresh and reruns its checks, as on its first
    event. Save_state replaces the file in one step, so a reader never sees half of one; any other failure to read it
    is raised to the caller, which reports it."""
    try:
        state = state_from(json.loads(in_data(session.state).read_text()))
    except (FileNotFoundError, json.JSONDecodeError, StateError):
        state = State()
    assert state.inherited in ("allow", "block"), f"state from {session.state} has gate {state.inherited}; make state_from refuse a gate other than allow or block"
    assert all(report.name for report in state.reports), f"state from {session.state} holds an unnamed report; make state_from refuse a report without a name"
    return state


def save_state(session: Session, state: State) -> None:
    """Replaces the state of session with state in one step, writing it first to a new temporary file that only this
    process can name or read, in the same directory, and removing that file if the write fails. Only process_event
    calls this, while it holds session.lock, for the session it read the state of."""
    text = json.dumps(state_json(state))
    with tempfile.NamedTemporaryFile("w", dir=session.state.parent, prefix=f"{session.state.stem}.", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(text)
        except OSError:
            temporary.unlink()
            raise
    assert temporary.parent == session.state.parent, f"state {session.state} was written through {temporary}, in another directory; write beside it"
    temporary.replace(session.state)
    assert not temporary.exists(), f"{temporary} was left behind; replace the state file with it"


def failing_line(failures: list[str]) -> str:
    """Names the failing tests, the first FAILURE_LIMIT of them, for the agent."""
    if not failures:
        return ""
    named = ", ".join(f"`{name}`" for name in failures[:FAILURE_LIMIT])
    more = f", and {len(failures) - FAILURE_LIMIT} more" if len(failures) > FAILURE_LIMIT else ""
    line = f"Failing: {named}{more}.\n"
    assert line.startswith(f"Failing: `{failures[0]}`"), f"line {line} does not name {failures[0]} first; quote the first {FAILURE_LIMIT} in order"
    assert (len(failures) > FAILURE_LIMIT) == line.endswith(" more.\n"), f"line {line} for {len(failures)} failures miscounts the rest"
    return line


def where(report: Report) -> str:
    """The sub-project a report's command ran in, for the agent."""
    text = f" in `{report.dir}`" if report.dir else ""
    assert bool(text) == bool(report.dir), f"where gave {text} for dir {report.dir}; name the dir only when there is one"
    assert not text or text.endswith(f"`{report.dir}`"), f"where gave {text}; quote the dir {report.dir}"
    return text


def key_of(name: str, directory: str | None) -> str:
    """What identifies a check across runs: its name and the sub-project it ran in, joined by a NUL neither can hold."""
    if "\0" in name or "\0" in (directory or ""):
        raise ConfigurationError(f"check {name!r} in {directory!r} holds a NUL character; remove it from its name in .overwatch.json")
    key = f"{name}\0{directory or ''}"
    assert key.split("\0") == [name, directory or ""], f"key {key!r} does not split back into {name!r} and {directory!r}"
    assert key.count("\0") == 1, f"key {key!r} holds {key.count(chr(0))} separators; join exactly two fields"
    return key


def report_key(report: Report) -> str:
    """The key of the check report came from."""
    key = key_of(report.name, report.dir)
    assert key.startswith(report.name), f"key {key!r} does not start with {report.name}; build it with key_of(name, dir)"
    assert key.endswith(report.dir or ""), f"key {key!r} does not end with {report.dir}; build it with key_of(name, dir)"
    return key


def known_failures(report: Report, baseline: Mapping[str, First]) -> list[str]:
    """The failures this check already had when the session started: none when its first result passed."""
    first = baseline.get(report_key(report))
    known = list(first.failures) if first is not None and first.failed else []
    assert first is None or first.failed or not known, f"{report.name} knows failures {known} from a passing baseline"
    assert first is None or not first.failed or len(known) == len(first.failures), f"{report.name} kept {len(known)} known failures; keep all of them"
    return known


def is_regression(report: Report, baseline: Mapping[str, First]) -> bool:
    """Whether a report fails in a way its baseline, the session's first result, did not."""
    if not report.failed or report.environment:
        return False
    first = baseline.get(report_key(report))
    known = set(known_failures(report, baseline))
    if first is None or not first.failed or any(name not in known for name in report.failures):
        return True
    previous, counts = failure_counts(first.summary), failure_counts(report.summary)
    grew = [kind for kind, count in counts.items() if count > previous.get(kind, 0)]
    assert first.failed and report.failed, f"{report.name} compared counts without two failing results; compare only failing ones"
    assert set(grew) <= set(counts), f"{report.name} grew in {grew}, which its summary does not count"
    return bool(grew)


def failure_counts(summary: str) -> dict[str, int]:
    """Each count a summary gives, keyed by its kind (error, warning or failure) and its place among the counts of
    that kind, so a summary with two failure counts, such as zig's failed steps and failed tests, compares each with
    its own."""
    counts: dict[str, int] = {}
    seen = {"error": 0, "warning": 0, "failure": 0}
    for number, label in re.findall(r"(\d+)\s+(errors?|warnings?|failures?|failed|fail)\b", summary):
        kind = "error" if label.startswith("error") else "warning" if label.startswith("warning") else "failure"
        counts[f"{kind} {seen[kind]}"] = int(number)
        seen[kind] += 1
    assert len(counts) == sum(seen.values()), f"summary {summary} gave {len(counts)} counts from {seen}; key each by its kind and place"
    assert all(str(count) in summary for count in counts.values()), f"counts {counts} are not in summary {summary}; take each from it"
    return counts


def write_log(session: Session, report: Report, prefix: str = "") -> Path:
    """Writes a report's full output into session.logs, under a name made from prefix and a digest of the report's
    key, replacing that check's earlier log, keeping the output out of the conversation. Only overwatch writes there,
    while it holds session.lock."""
    log = session.logs / f"{prefix}{hashlib.sha256(report_key(report).encode()).hexdigest()[:16]}.log"
    assert log.parent == session.logs, f"log {log} is outside {session.logs}; name it by digest alone"
    log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    log.write_text(report.text)
    assert log.read_text() == report.text, f"{log} does not hold {report.name}'s output as given; write it whole"
    return log


def failure_message(reports: list[Report], baseline: Mapping[str, First], logs: dict[str, Path]) -> str:
    """Tells the agent which checks newly fail, what fails in each and where the full output is."""
    assert all(report_key(report) in logs for report in reports), f"no log for {[report.name for report in reports if report_key(report) not in logs]}; write one for each report"
    messages: list[str] = []
    for report in reports:
        known = set(known_failures(report, baseline))
        fresh = [name for name in report.failures if name not in known]
        old = len(report.failures) - len(fresh)
        messages.append(
            f"overwatch: `{report.command}`{where(report)} failed after the latest change: {report.summary}\n"
            f"{failing_line(fresh)}"
            f"{f'{old} more were already failing when the session started.' + chr(10) if old else ''}"
            "Fix these new failures before finishing. Tools remain available while you investigate.\n"
            f"Full output: `{logs[report_key(report)]}`"
        )
    text = "\n\n".join(messages)
    assert text.count("Full output: ") == len(reports), f"message links {text.count('Full output: ')} logs for {len(reports)} reports; link each once"
    return text


def environment_message(reports: list[Report]) -> str:
    """Tells the agent which checks cannot run where they are, and that no code change fixes that."""
    assert all(report.environment for report in reports), f"{[report.name for report in reports if not report.environment]} have no environment line; pass only checks that cannot run"
    lines = "\n".join(f"- `{report.command}`{where(report)}: {report.environment}" for report in reports)
    assert lines.count("\n- ") == max(len(reports) - 1, 0), f"environment message lists {lines.count(chr(10) + '- ') + 1} lines for {len(reports)} checks"
    return (
        "overwatch: these checks cannot run in this environment, so they are not reporting. "
        f"This is not caused by your changes and needs no code change:\n{lines}"
    )


def failing_suffix(failures: tuple[str, ...]) -> str:
    """The first failure and how many more, for the status line."""
    if not failures:
        return ""
    more = f" +{len(failures) - 1} more" if len(failures) > 1 else ""
    suffix = f": {failures[0]}{more}"
    assert suffix.startswith(f": {failures[0]}"), f"suffix {suffix} does not name {failures[0]} first"
    assert (len(failures) > 1) == suffix.endswith(" more"), f"suffix {suffix} for {len(failures)} failures miscounts the rest"
    return suffix


def failure_status(reports: list[Report]) -> str:
    """The status line for failing checks, one part each."""
    assert reports, "failure_status was given no reports; pass the checks that newly fail"
    summaries = " · ".join(
        f"{report.name} ✗ {SUMMARY_DECORATION.sub('', report.summary)}{failing_suffix(report.failures)}"
        for report in reports
    )
    assert summaries.count(" ✗ ") >= len(reports), f"status {summaries} marks fewer than {len(reports)} checks; give each one part"
    return f"Overwatch: {summaries}"


def recovery_message(reports: tuple[Report, ...]) -> str:
    """Tells the agent the checks pass again, with each one's summary."""
    summaries = " · ".join(f"overwatch {report.name} ✓ {SUMMARY_DECORATION.sub('', report.summary)}" for report in reports)
    assert summaries.count(" ✓ ") >= len(reports), f"recovery {summaries} marks fewer than {len(reports)} checks; give each one part"
    text = f"overwatch: green again. {summaries}"
    assert text.endswith(summaries), f"recovery {text} drops the summaries; end with them"
    return text


def context_output(event_name: str, text: str) -> dict[str, Any]:
    """The hook output that adds text to the agent's context for event_name."""
    assert text, f"context for {event_name} is empty; add context only when there is something to say"
    output = {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": text,
        }
    }
    assert json.loads(json.dumps(output)) == output, f"context for {event_name} is not plain JSON; pass text as a string"
    return output


def report_state(reports: list[Report] | tuple[Report, ...], old: State) -> State:
    """The state after reports: the baseline with each new check's first result, and the regressions against it."""
    baseline = dict(old.baseline)
    for report in reports:
        if not report.environment:
            baseline.setdefault(report_key(report), First(report.failed, report.failures, report.summary))
    regressions = [report for report in reports if is_regression(report, baseline)]
    signatures = regression_signatures(regressions, baseline)
    assert set(old.baseline) <= set(baseline), f"baseline lost {set(old.baseline) - set(baseline)}; keep each check's first result"
    assert bool(signatures) == bool(regressions), f"{len(regressions)} regressions gave signatures {signatures}; sign each one"
    return replace(old, reports=tuple(reports), baseline=baseline, failed=bool(regressions),
                   told=json.dumps(signatures) if regressions else None, signatures=tuple(signatures))


def regression_signatures(reports: list[Report], baseline: Mapping[str, First]) -> list[str]:
    """What identifies each regression, the new failures or else the summary without timings, sorted."""
    signatures: list[str] = []
    for report in reports:
        known = set(known_failures(report, baseline))
        fresh = [name for name in report.failures if name not in known]
        identities = fresh or [re.sub(r"\d+(?:\.\d+)?m?s\b", "", report.summary)]
        signatures.extend(json.dumps([report_key(report), identity]) for identity in identities)
    assert len(signatures) >= len(reports), f"{len(reports)} regressions gave {len(signatures)} signatures; sign each at least once"
    assert all(json.loads(signature)[0] in {report_key(report) for report in reports} for signature in signatures), f"signatures {signatures} name a check that did not regress"
    return sorted(signatures)


def block_stop(event: Event, current: State) -> bool:
    """Whether a Stop event is refused because guarded checks fail."""
    if event.hook_event_name != "Stop":
        return False
    guarded = gate_reports(current)
    assert all(report in current.reports for report in guarded), f"guarded {[report.name for report in guarded]} are not this run's reports; select them from current.reports"
    assert all(report.failed for report in guarded), f"{[report.name for report in guarded if not report.failed]} pass but block Stop; gate_reports must keep only failures"
    return bool(guarded)


def gate_reports(current: State, checks: tuple[str, ...] | list[str] | None = None) -> list[Report]:
    """The failing reports that guard an action: those among checks, inherited ones only when the policy blocks them."""
    inherited = current.inherited == "block"
    guarded = [report for report in current.reports
               if report.failed and not report.environment
               and (not checks or report.name in checks or report.language in checks)
               and (inherited or is_regression(report, current.baseline))]
    assert len(guarded) <= len(current.reports), f"guarded {len(guarded)} of {len(current.reports)} reports; select from them"
    assert not any(report.environment for report in guarded), f"{[report.name for report in guarded if report.environment]} cannot run but guard; leave them out"
    return guarded


def protected_output(event: Event, cwd: Path, session: Session) -> dict[str, Any]:
    """The PreToolUse output: a denial when the tool call is protected and its checks fail, else nothing. Saves the
    rechecked state in session, keeping what the agent was told, since a tool call tells it nothing."""
    checks = protected_checks(event.tool_name, tool_command(event.tool_input), read_gate(read_override(cwd)))
    if checks is None:
        return {}
    old, current = checked_state(session, cwd)
    save_state(session, replace(current, told=old.told))
    reports = gate_reports(current, checks)
    if not reports:
        return {}
    reason = gate_reason(reports)
    assert all(report.failed for report in reports), f"{[report.name for report in reports if not report.failed]} pass but deny the call"
    assert all(not checks or report.name in checks or report.language in checks for report in reports), f"{[report.name for report in reports]} guard a call protected only by {checks}"
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def gate_reason(reports: list[Report]) -> str:
    """Why an action is refused: the failing checks that guard it, and that repairs remain possible."""
    assert reports, "gate_reason was given no reports; give it the checks that guard the action"
    lines = [f"- {report.name}: {' '.join(report.summary.split())}" for report in reports]
    assert len(lines) == len(reports) and all("\n" not in line for line in lines), f"reason lines {lines} do not give each guarding check one line"
    return ("overwatch: this action is guarded while relevant checks fail:\n" + "\n".join(lines) + "\n"
            "Inspection, edits, and check commands remain available. Rerun the checks after repairing the failure.")


def inventory_checks(session: Session) -> dict[str, dict[str, object]]:
    """The entries the session's triage inventory already lists, by their check's key, as triage.py parses them; none
    when there is no inventory yet, or when it is not one triage.py can read, which baseline_inventory then rewrites
    whole. Any other failure to read it is raised to the caller, which reports it."""
    try:
        value = json.loads(in_data(session.inventory).read_text())
        parse_inventory(value)
    except (FileNotFoundError, json.JSONDecodeError, InventoryError):
        return {}
    entries = {key_of(check.name, check.dir): entry for entry in value["checks"] for check in [parse_check(entry)]}
    assert len(entries) <= len(value["checks"]), f"inventory {session.inventory} gave {len(entries)} keys for {len(value['checks'])} entries"
    assert all("\0" in key for key in entries), f"inventory {session.inventory} keys {list(entries)} were not built with key_of"
    return entries


def inherited_failures(old: State, current: State) -> list[Report]:
    """The reports of checks that failed on their first run in this session, for reasons in the code."""
    added = [report for report in current.reports if report_key(report) not in old.baseline]
    failing = [report for report in added if report.failed and not report.environment]
    assert all(report in current.reports for report in failing), f"inherited failures {[report.name for report in failing]} are not this run's; select them from current.reports"
    assert not any(report_key(report) in old.baseline for report in failing), f"{[report.name for report in failing]} already had a baseline; leave out checks old.baseline holds"
    return failing


def baseline_inventory(old: State, current: State, session: Session) -> str:
    """Keeps the inherited backlog inspectable without putting it in model context: writes session.inventory, the
    entries it already lists for other checks followed by one for each check that first failed in this run, each
    linking its full log, so a repeated event leaves one entry per check. Only overwatch writes the inventory, while
    it holds session.lock."""
    added = inherited_failures(old, current)
    if not added:
        return ""
    inventory = session.inventory
    keys = {report_key(report): report for report in added}
    checks = [entry for key, entry in inventory_checks(session).items() if key not in keys]
    checks.extend({**{name: value for name, value in asdict(report).items() if name != "text"},
                   "log": str(write_log(session, report, "baseline-"))} for report in keys.values())
    inventory.write_text(json.dumps({"checks": checks}, indent=2))
    assert len(checks) >= len(added), f"inventory {inventory} holds {len(checks)} checks after adding {len(added)}; append every one"
    assert all("text" not in check for check in checks[-len(added):]), f"inventory {inventory} kept a report's output; link its log instead"
    count = sum(len(report.failures) for report in added)
    detail = f"{count} named issues" if count else "individual issue names unavailable; see full logs"
    policy = ("Inherited findings guard completion and protected actions. "
              if current.inherited == "block"
              else "It is not a Stop blocker; only new failures are guarded. ")
    return (f"overwatch: inherited backlog in {len(added)} checks ({detail}). "
            f"{policy}"
            f"Triage inventory: `{inventory}`. Filter or page it with hooks/triage.py; full logs are linked there.")


def new_environment(reports: tuple[Report, ...] | list[Report], told: tuple[str, ...]) -> list[Report]:
    """The reports that cannot run for a reason the agent has not been told yet."""
    fresh = [report for report in reports if report.environment and report.environment not in told]
    assert all(report in reports for report in fresh), f"new environment reports {[report.name for report in fresh]} are not among the reports; select them from reports"
    assert not any(report.environment in told for report in fresh), f"{[report.environment for report in fresh]} repeat what the agent was told; leave out lines in told"
    return fresh


def process_event(event: Event) -> dict[str, Any]:
    """The hook output for one event, saving the state it leaves. Hooks of one session can run at once, so each
    holds an exclusive flock on the state's .lock sibling from reading the state to saving it: the next one waits,
    then sees the saved fingerprint and reuses the results instead of running the checks again."""
    cwd = repository_of(Path(event.cwd or os.getcwd()).resolve())
    if cwd is None:
        return {}
    path = state_path(event, cwd)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    session = session_at(path)
    with open(session.lock, "a") as lock:
        hold(lock)
        if event.hook_event_name == "PreToolUse":
            return protected_output(event, cwd, session)
        old, current = checked_state(session, cwd)
        output = report_output(event, old, current, session)
        save_state(session, current)
    assert current.fingerprint is not None, f"state for {cwd} has no fingerprint; checked_state must record one"
    assert session.state.exists(), f"no state was saved for {cwd} at {session.state}; save_state must write it"
    return output


def hold(lock: Any) -> None:
    """Waits for, then holds, an exclusive flock on the open lock file, until the file is closed. The one place that
    locks, so the POSIX call is here alone."""
    fcntl.flock(lock, fcntl.LOCK_EX)
    assert not lock.closed, f"lock {lock.name} closed while it was taken; hold it open for the whole event"
    assert lock.name.endswith(".lock"), f"took a lock on {lock.name}; lock the session's .lock file"


def checked_state(session: Session, cwd: Path) -> tuple[State, State]:
    """The state saved in session and the state after rerunning the checks when the workspace changed since it was saved."""
    old = load_state(session)
    gate = read_gate(read_override(cwd))
    before = fingerprint(cwd)
    should_run = old.fingerprint != before
    reports = run_checks(cwd, find_watchers(cwd)) if should_run else old.reports
    told = old.environment
    environment = new_environment(reports, told)
    current = replace(
        report_state(reports, old),
        inherited=gate.inherited,
        fingerprint=fingerprint(cwd) if should_run else before,
        environment=(*told, *(report.environment for report in environment if report.environment)),
    )
    assert current.environment[:len(told)] == told, f"environment {current.environment} forgot what the agent was told; keep {told} first"
    assert should_run or current.reports == old.reports, f"unchanged workspace {cwd} got new reports; reuse the saved ones"
    return old, current


def report_output(event: Event, old: State, current: State, session: Session) -> dict[str, Any]:
    """The hook output for the new state: what newly fails, recovers or cannot run, and a Stop refusal when guarded."""
    reports = current.reports
    environment = new_environment(reports, old.environment)
    baseline_notice = baseline_inventory(old, current, session)
    parts: list[str] = [baseline_notice] if baseline_notice else []
    output: dict[str, Any] = {}
    blocking = block_stop(event, current)
    if current.failed and current.told != old.told:
        regressions = [report for report in reports if is_regression(report, current.baseline)]
        logs = {report_key(report): write_log(session, report) for report in regressions}
        message = failure_message(regressions, current.baseline, logs)
        parts.append(message)
        output["systemMessage"] = failure_status(regressions)
    elif not current.failed and old.failed:
        parts.append(recovery_message(reports))
    if environment:
        parts.append(environment_message(environment))
        output.setdefault("systemMessage", "Overwatch: some checks cannot run in this environment")
    if blocking:
        output.update({"decision": "block", "reason": gate_reason(gate_reports(current))})
    if parts:
        output.update(context_output(event.hook_event_name, "\n\n".join(parts)))
    assert blocking == (output.get("decision") == "block"), f"output {output} disagrees with blocking={blocking}; block only a guarded Stop"
    assert bool(parts) == ("hookSpecificOutput" in output), f"output {output} drops or invents context for {len(parts)} parts"
    return output


def main() -> int:
    """Answers one hook event on stdin with JSON on stdout. The event comes from the agent host that started this
    process, through the pipe it holds, so no one else can write it. A hook's exit code is not where a host reads
    problems, and a failing exit would stop the agent's turn, so this always exits 0 and reports any problem as a
    system message in its output instead: a configuration error as itself, anything else as an unexpected error."""
    try:
        output = json.dumps(process_event(read_event()))
        assert output.startswith("{"), f"hook output {output[:40]} is not a JSON object; return a dict from process_event"
        assert "\n" not in output, "hook output spans lines; print it as one JSON line"
        print(output)
        return 0
    except ConfigurationError as error:
        print(json.dumps({"systemMessage": f"overwatch: {error}"}))
        return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({"systemMessage": f"overwatch: unexpected error: {error}"}))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
