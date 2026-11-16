"""TCR's two git operations: commit the whole workspace after a green result, and restore the latest commit after a
red one. Neither decides when to run; the hook that calls them does. Both refuse, before changing anything, in a
repository where either would do harm: an operation such as a merge is in progress, or an untracked repository sits
inside the workspace.

Copyright (c) 2026 benomahony. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import codecs
import contextlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path

MINIMUM_PYTHON = (3, 11)
if sys.version_info < MINIMUM_PYTHON:
    print(json.dumps({"error": f"overwatch needs python {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]} or newer; python3 on PATH is {sys.version_info[0]}.{sys.version_info[1]}"}))
    raise SystemExit(1)

from tcr_settings import validate_allow

# How long a git command may take, in seconds. A commit runs the repository's own hooks, which can be slow.
GIT_TIMEOUT = 30
# How many file names a checkpoint message lists before it says how many more there were.
MESSAGE_FILES = 5
MESSAGE_PREFIX = "tcr: "
# A staged file larger than this is refused: 50 MiB is where GitHub starts to warn about a file.
MAX_STAGED_BYTES = 50 * 1024 * 1024
ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})
PRIVATE_KEY_NAMES = frozenset({"id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"})
CREDENTIAL_NAMES = frozenset({"credentials", "credentials.json", "credentials.yml", "credentials.yaml"})
SECRET_FILE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")
SECRET_FILE_NAMES = frozenset({".npmrc", ".netrc", ".pgpass", ".git-credentials", "terraform.tfstate", "secrets.yml", "secrets.yaml", "kubeconfig"})
NAME_RULE = "name pattern"
UNREADABLE_RULE = "its added lines could not be read, so they were not screened for secrets"
SKIP_WORKTREE_TAG = "S"
SIZE_RULE = f"larger than {MAX_STAGED_BYTES // (1024 * 1024)} MiB"
# Only secrets that have an unmistakable form: a generic `key = "..."` pattern flagged too much ordinary code.
CONTENT_RULES = (
    ("AWS access key", r"AKIA[0-9A-Z]{16}"),
    ("private key", r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP |ENCRYPTED )?PRIVATE KEY"),
    ("GitHub token", r"gh[pousr]_[A-Za-z0-9]{36,}"),
    ("Slack token", r"xox[baprs]-"),
    ("Stripe live key", r"sk_live_"),
    ("Anthropic key", r"sk-ant-"),
    ("Google API key", r"AIza[0-9A-Za-z_-]{35}"),
)
CONTENT_PATTERN = re.compile("|".join(f"(?P<rule{index}>{pattern})" for index, (_, pattern) in enumerate(CONTENT_RULES)))
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# How many times a revert saves the files again when they changed while it saved them.
MAX_SAVE_ATTEMPTS = 3
# How many paths one git clean is given, so that a long list stays under the limit on the length of a command.
CLEAN_BATCH = 200
SAVED_REF_PREFIX = "refs/overwatch/reverted/"
SAVED_REF_TIME = "%Y%m%dT%H%M%S%fZ"
SAVED_MESSAGE = "overwatch: work that a TCR revert discarded"
# Who the saved commit is by when git knows no one: the commit only holds the discarded work, and is nobody's own.
FALLBACK_IDENTITY = (("GIT_AUTHOR_NAME", "overwatch"), ("GIT_AUTHOR_EMAIL", "overwatch@localhost"),
                     ("GIT_COMMITTER_NAME", "overwatch"), ("GIT_COMMITTER_EMAIL", "overwatch@localhost"))
UNWATCHED_REASON = "{files} is marked assume-unchanged or skip-worktree and differs from HEAD, so git does not show the edit and a revert would overwrite it"
# Files and directories in the git directory that exist while an operation is unfinished, with its name.
IN_PROGRESS = {"MERGE_HEAD": "merge", "CHERRY_PICK_HEAD": "cherry-pick", "REVERT_HEAD": "revert",
               "BISECT_LOG": "bisect", "rebase-merge": "rebase", "rebase-apply": "rebase"}


# Variables that point git at a repository other than the one in the working directory. Git sets some of them for
# its own hooks, so they can be in the environment overwatch runs in, and a reset would then act on the wrong repository.
GIT_LOCATION_VARIABLES = frozenset({"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY"})


class CheckpointError(Exception):
    """A git operation of the checkpoint failed or was refused; the message says why and what was left as it was."""


class Refused(CheckpointError):
    """The checkpoint would have committed files it must not: refusals lists each one. Nothing was committed, and the
    files stay in the work tree, unstaged."""

    def __init__(self, refusals: list[Refusal]) -> None:
        assert refusals, "a checkpoint was refused with nothing to refuse; raise Refused only for a refusal"
        self.refusals = refusals
        super().__init__(f"no checkpoint: it would commit {', '.join(refusal.text() for refusal in refusals)}; nothing was committed or reverted")
        assert self.refusals is refusals, "Refused lost the refusals it was made with"


class Unreadable(CheckpointError):
    """A diff of the staged files was not in the form this reader knows, so the lines it adds cannot be screened."""


@dataclass(frozen=True)
class Refusal:
    """One reason a checkpoint would not commit a file: the rule it broke, and the line of it when a line did."""
    path: str
    rule: str
    line: int | None = None

    def text(self) -> str:
        """The refusal as the agent and the user read it: `.env (name pattern)` or `src/a.py:12 (AWS access key)`."""
        place = self.path if self.line is None else f"{self.path}:{self.line}"
        assert self.path and self.rule, f"a refusal of {self.path!r} for {self.rule!r} names nothing; give both"
        assert self.line is None or self.line > 0, f"line {self.line} of {self.path} is no line number"
        return f"{place} ({self.rule})"


@dataclass(frozen=True)
class GitIO:
    """What a git command reads besides its arguments: the text on its standard input, and the index file it uses in
    place of the repository's own, and variables it needs besides the process's."""
    stdin: str | None = None
    index: Path | None = None
    environment: tuple[tuple[str, str], ...] = ()


def run_git(cwd: Path, argv: list[str], *, warnings_fail: bool = False, io: GitIO = GitIO()) -> str:
    """Runs git with argv in cwd and returns what it printed, or raises CheckpointError with git's own message. Bytes
    that are not UTF-8, as in a file name Linux allows, come back as the surrogate characters that encode them again,
    and a carriage return comes back as one: no line ending is translated, so a line break is only a line feed.
    Git finds the repository from cwd alone, whatever location variables the environment holds. With warnings_fail, a
    command that exits 0 but wrote to stderr, as git does for a directory it cannot read, fails too. io gives it
    input and an index file of its own."""
    environment = {name: value for name, value in os.environ.items() if name not in GIT_LOCATION_VARIABLES}
    assert not GIT_LOCATION_VARIABLES & set(environment), f"git's environment still names a repository: {GIT_LOCATION_VARIABLES & set(environment)}"
    environment.update(io.environment)
    if io.index is not None:
        environment["GIT_INDEX_FILE"] = str(io.index)
    command = " ".join(argv)
    try:
        result = subprocess.run(["git", *argv], cwd=cwd, env=environment, capture_output=True, timeout=GIT_TIMEOUT, check=False,
                                input=None if io.stdin is None else io.stdin.encode(errors="surrogateescape"))
    except (OSError, subprocess.SubprocessError) as error:
        raise CheckpointError(f"git {command} could not run in {cwd}: {error}; check that git is installed") from error
    stdout = result.stdout.decode(errors="surrogateescape")
    stderr = result.stderr.decode(errors="surrogateescape").strip()
    if result.returncode != 0:
        raise CheckpointError(f"git {command} failed in {cwd}: {stderr or stdout.strip() or f'exit {result.returncode}'}")
    if warnings_fail and stderr:
        raise CheckpointError(f"git {command} warned in {cwd}: {stderr}")
    assert result.returncode == 0, f"git {argv} exited {result.returncode} but was not reported; raise CheckpointError"
    assert isinstance(stdout, str), f"git {argv} printed {type(stdout)}, not text"
    return stdout


def repository_root(cwd: Path) -> Path:
    """The top directory of the repository holding cwd, which every command here runs in so that none of them
    sees only a subdirectory. Raises CheckpointError outside a repository."""
    root = Path(run_git(cwd, ["rev-parse", "--show-toplevel"]).strip())
    assert root.is_absolute(), f"git gave the relative top directory {root} for {cwd}; expected an absolute path"
    assert root.resolve() == cwd.resolve() or root.resolve() in cwd.resolve().parents, f"{root} does not hold {cwd}"
    return root


def operation_in_progress(root: Path) -> str | None:
    """The name of the unfinished operation, such as merge or rebase, whose files are in the repository's git
    directory; None when there is none. A hard reset or a commit of everything would wreck it."""
    found: str | None = None
    for marker, name in IN_PROGRESS.items():
        listed = run_git(root, ["rev-parse", "--git-path", marker]).strip()
        located = Path(listed) if Path(listed).is_absolute() else root / listed
        if found is None and located.exists():
            found = name
    assert found is None or found in IN_PROGRESS.values(), f"found {found!r}, which no marker of {IN_PROGRESS} names"
    assert found is None or any(found == name for name in IN_PROGRESS.values()), f"{found!r} is not an operation name"
    return found


def nested_repositories(root: Path) -> list[str]:
    """The untracked directories, at any depth, in the repository at root that are repositories themselves: git lists
    one as a directory only because it is a repository. A commit of everything would record each as an empty link,
    and a clean would skip it."""
    untracked = run_git(root, ["ls-files", "--others", "--exclude-standard", "-z"]).split("\0")
    nested = sorted(name for name in untracked if name.endswith("/") and (root / name / ".git").exists())
    assert all(name.endswith("/") for name in nested), f"{nested} include a name that is no directory"
    assert len(nested) <= len(untracked), f"{len(nested)} nested repositories among {len(untracked)} untracked paths"
    return nested


def submodules(root: Path) -> list[str]:
    """The paths of the submodules of the repository at root. A checkpoint can record only which commit each one is
    at, not the files inside it, and a hard reset does not go into them."""
    staged = run_git(root, ["ls-files", "--stage", "-z"]).split("\0")
    paths = sorted(entry.split("\t", 1)[1] for entry in staged if entry.startswith("160000 ") and "\t" in entry)
    assert all(not path.startswith("/") for path in paths), f"{paths} include an absolute path; git lists paths from the root"
    assert len(paths) <= len(staged), f"{len(paths)} submodules among {len(staged)} entries"
    return paths


def has_commit(root: Path) -> bool:
    """Whether the repository at root has a commit for HEAD to name."""
    assert root.is_dir(), f"{root} is not a directory to look for a commit in"
    try:
        named = run_git(root, ["rev-parse", "--verify", "-q", "HEAD"]).strip()
    except CheckpointError:
        return False
    assert len(named) in (40, 64), f"HEAD in {root} names {named!r}, which is neither a SHA-1 nor a SHA-256 object ID"
    return True


def on_branch(root: Path) -> bool:
    """Whether HEAD names a branch rather than a bare commit, which a commit made here would leave behind."""
    assert root.is_dir(), f"{root} is not a directory to look for a branch in"
    try:
        branch = run_git(root, ["symbolic-ref", "-q", "HEAD"]).strip()
    except CheckpointError:
        return False
    assert branch.startswith("refs/heads/"), f"HEAD in {root} names {branch!r}, which is no branch"
    return True


def is_clean(cwd: Path) -> bool:
    """Whether the repository at cwd has no change to a tracked file and no new file that its ignore rules leave
    out. A directory outside any repository raises CheckpointError."""
    listed = run_git(cwd, ["status", "--porcelain", "--untracked-files=all"]).strip()
    clean = listed == ""
    assert clean == (len(listed.splitlines()) == 0), f"status listed {listed!r} but clean is {clean}"
    assert isinstance(clean, bool), f"clean is {type(clean)}, not a yes or no"
    return clean


def unavailable_reason(cwd: Path, *, dirty_ok: bool = False) -> str | None:
    """Why TCR cannot start in the repository holding cwd, as one sentence, or None when it can. It starts only on a
    branch that has a commit and a clean tree, with no operation in progress and no untracked nested repository, so
    that no revert can discard work that was there before it. With dirty_ok, uncommitted changes are no reason: the
    caller knows whose they are."""
    root = repository_root(cwd)
    operation = operation_in_progress(root)
    nested = nested_repositories(root)
    committed = has_commit(root)
    clean = is_clean(root) or dirty_ok
    changed = uncommitted(root) if committed and not clean else []
    unwatched = unwatched_edits(root) if committed else []
    reasons = [
        (not committed, "the repository has no commit to restore"),
        (committed and not on_branch(root), "HEAD is detached, so a checkpoint would belong to no branch"),
        (operation is not None, f"a {operation} is in progress"),
        (bool(unwatched), UNWATCHED_REASON.format(files=named(unwatched))),
        (bool(nested), f"the untracked nested repository {', '.join(nested)} would not be restored"),
        (not clean, "the working tree has uncommitted changes that a revert would discard" + (f": {named(changed)}" if changed else "")),
    ]
    reason = next((text for applies, text in reasons if applies), None)
    assert reason is None or any(reason == text for _, text in reasons), f"reason {reason!r} is not one of the listed reasons"
    assert (reason is None) == (not any(applies for applies, _ in reasons)), f"reason {reason!r} disagrees with the checks"
    return reason


def uncommitted(root: Path) -> list[str]:
    """What still differs from HEAD in the repository at root: changed tracked files and untracked files, leaving out
    the files inside submodules and untracked nested repositories, which neither a checkpoint nor a revert covers."""
    inside = set(submodules(root))
    kept = set(nested_repositories(root))
    tracked = [name for name in run_git(root, ["diff", "--name-only", "-z", "HEAD"]).split("\0") if name and name not in inside]
    untracked = [name for name in run_git(root, ["ls-files", "--others", "--exclude-standard", "-z"]).split("\0") if name and name not in kept]
    assert not set(tracked) & inside, f"{set(tracked) & inside} are submodules but were listed as changed files"
    assert not set(untracked) & kept, f"{set(untracked) & kept} are nested repositories but were listed as untracked files"
    return tracked + untracked


def unwatched_edits(root: Path) -> list[str]:
    """The tracked files that git was told not to look at, as `assume-unchanged` or `skip-worktree` does, and that differ
    from HEAD now: a person's edit to one is not in `git status`, and a hard reset would overwrite it. A flagged file
    that is not in the work tree, as in a sparse checkout, or that equals HEAD, holds nothing to lose."""
    entries = [entry for entry in run_git(root, ["ls-files", "-v", "-z"]).split("\0") if len(entry) > 2]
    flagged = [entry[2:] for entry in entries if entry[0].islower() or entry[0] == SKIP_WORKTREE_TAG]
    edited = []
    for name in flagged:
        if not (root / name).is_file():
            continue
        try:
            committed = run_git(root, ["rev-parse", "--verify", "-q", f"HEAD:{name}"]).strip()
        except CheckpointError:
            committed = ""
        if run_git(root, ["hash-object", "--", name]).strip() != committed:
            edited.append(name)
    assert all((root / name).is_file() for name in edited), f"{edited} are listed as edited but are not files in {root}"
    assert len(edited) <= len(flagged), f"{len(edited)} edited files among {len(flagged)} flagged ones"
    return edited


def dirty_submodules(root: Path) -> list[str]:
    """The submodules of the repository at root whose own files differ from what the repository records: neither a
    checkpoint nor a revert goes into them."""
    inside = set(submodules(root))
    entries = run_git(root, ["status", "--porcelain=v1", "-z", "--ignore-submodules=none"]).split("\0")
    dirty = sorted({entry[3:] for entry in entries if len(entry) > 3 and entry[3:] in inside})
    assert set(dirty) <= inside, f"{set(dirty) - inside} are listed as submodules but are none"
    assert len(dirty) <= len(inside), f"{len(dirty)} dirty submodules among {len(inside)}"
    return dirty


def named(files: list[str]) -> str:
    """Up to MESSAGE_FILES file names on one line, then how many more there were."""
    shown = [name.replace("\n", " ").replace("\t", " ") for name in files[:MESSAGE_FILES]]
    hidden = len(files) - len(shown)
    text = ", ".join(shown) + (f" and {hidden} more" if hidden > 0 else "")
    assert "\n" not in text, f"the names {text!r} are not on one line"
    assert hidden == 0 or text.endswith(f"and {hidden} more"), f"names {text!r} do not say that {hidden} files were left out"
    return text


def message(files: list[str]) -> str:
    """The checkpoint's commit message: tcr: then up to MESSAGE_FILES file names, then how many more there were."""
    text = MESSAGE_PREFIX + named(files)
    assert text.startswith(MESSAGE_PREFIX) and "\n" not in text, f"message {text!r} is not one tcr: line"
    assert text.count(MESSAGE_PREFIX) == 1 or any(MESSAGE_PREFIX in name for name in files), f"message {text!r} repeats the prefix"
    return text


def current_branch(root: Path) -> str | None:
    """The name of the branch HEAD names; None on a detached HEAD."""
    try:
        branch = run_git(root, ["symbolic-ref", "--short", "-q", "HEAD"]).strip()
    except CheckpointError:
        return None
    assert branch and "\n" not in branch, f"HEAD in {root} names the branch {branch!r}, which is no name"
    assert on_branch(root), f"{branch!r} was named as the branch, but HEAD in {root} is detached"
    return branch


def refuse_unsafe(root: Path, action: str) -> None:
    """Raises CheckpointError when an operation is in progress, so that action, a commit or a revert, changes nothing."""
    operation = operation_in_progress(root)
    if operation is not None:
        raise CheckpointError(f"no {action}: a {operation} is in progress in {root}; finish or abort it first")
    assert operation is None, f"a {operation} in progress was not refused"
    assert root.is_dir(), f"{root} is not a directory to {action} in"


def name_rule(path: str) -> str | None:
    """NAME_RULE when the file's name says it holds secrets, as `.env`, a private key or a credentials file does;
    None for any other name, and for a template such as `.env.example` or a public key."""
    name = path.rsplit("/", 1)[-1].lower()
    if name in ENV_TEMPLATES or name.endswith(".pub"):
        return None
    private_key = any(name == key or name.startswith(f"{key}.") for key in PRIVATE_KEY_NAMES)
    secret = (name == ".env" or name.startswith(".env.") or name.endswith(SECRET_FILE_SUFFIXES) or name in SECRET_FILE_NAMES
              or private_key or name in CREDENTIAL_NAMES)
    assert not secret or not name.endswith(".pub"), f"{path} is a public key but was refused as a secret"
    assert not secret or name not in ENV_TEMPLATES, f"{path} is a template but was refused as a secret"
    return NAME_RULE if secret else None


def matches_path(parts: list[str], pattern: list[str]) -> bool:
    """Whether the path, split at its slashes, fits the pattern, split the same way: a part of the pattern is a glob for
    one part of the path, which a star does not cross, and a part that is two stars fits any number of parts."""
    reached, last = {0}, len(parts)
    for token in pattern:
        if token == "**":
            reached = {position for start in reached for position in range(start, last + 1)}
        else:
            reached = {position + 1 for position in reached if position < last and fnmatchcase(parts[position], token)}
    assert all(0 <= position <= last for position in reached), f"the pattern {pattern} reached a part beyond the path {parts}"
    assert not reached or min(reached) >= (0 if "**" in pattern else len(pattern)), f"the pattern {pattern} reached {reached} in {parts} without matching each part"
    return last in reached


def is_allowed(path: str, allow: tuple[str, ...]) -> bool:
    """Whether an allowlist entry names the file. An entry is a path from the repository's root, and a glob in it fits
    one directory or file name at a time; a name without a directory fits only that name at the root, and `**/` before
    it fits the name in any directory."""
    parts = path.split("/")
    allowed = any(matches_path(parts, entry.split("/")) for entry in allow)
    assert not allowed or allow, f"{path} is allowed by an empty allowlist"
    assert all(parts), f"{path!r} has an empty directory or file name to allow"
    return allowed


def staged_files(root: Path) -> list[tuple[str, str, str]]:
    """The mode, blob and path of each file the index adds or changes against HEAD, in git's order."""
    fields = run_git(root, ["diff", "--cached", "--raw", "--no-abbrev", "-z", "--no-renames", "--diff-filter=AMT"]).split("\0")
    assert fields[-1] == "", f"git ended its list of staged files with {fields[-1]!r}, not a NUL"
    headers, names = fields[:-1:2], fields[1:-1:2]
    assert len(headers) == len(names) and all(header.startswith(":") for header in headers), f"git listed headers and paths that do not pair up: {fields}"
    files = [(header.split()[1], header.split()[3], name) for header, name in zip(headers, names)]
    assert all(len(blob) >= 40 for _, blob, _ in files), f"git listed a blob that is no object ID in {files}"
    assert all(path for _, _, path in files), f"git listed a staged file with no path in {files}"
    return files


def oversized(root: Path, files: list[tuple[str, str, str]]) -> list[Refusal]:
    """The files among the staged ones, leaving out submodules, whose content is larger than MAX_STAGED_BYTES."""
    blobs = [(blob, path) for mode, blob, path in files if mode != "160000"]
    if not blobs:
        return []
    sizes = run_git(root, ["cat-file", "--batch-check=%(objectsize)"], io=GitIO(stdin="".join(f"{blob}\n" for blob, _ in blobs))).split()
    assert len(sizes) == len(blobs), f"git gave {len(sizes)} sizes for {len(blobs)} blobs"
    found = [Refusal(path, SIZE_RULE) for (_, path), size in zip(blobs, sizes) if size.isdigit() and int(size) > MAX_STAGED_BYTES]
    assert all(refusal.path in {path for _, path in blobs} for refusal in found), f"{found} name a file that was not staged"
    return found


def header_path(header: str) -> str:
    """The path that a `+++ b/path` header of a diff names. Git ends the header with a tab when the path holds a space,
    and writes a path with a quote or a control character between double quotes, with backslash escapes."""
    path = header.rstrip("\t")
    if path.startswith('"') and path.endswith('"') and len(path) > 1:
        path = codecs.escape_decode(path[1:-1].encode("utf-8"))[0].decode("utf-8", "surrogateescape")
    path = path.removeprefix("b/")
    assert path and not path.endswith("\t"), f"the header {header!r} names no path"
    assert not path.startswith("b/") or header.startswith('"'), f"the header {header!r} kept its b/ prefix"
    return path


def added_lines(diff: str) -> Iterator[tuple[str, int, str]]:
    """The path, the line number in the new file and the text of each line that a zero-context diff adds. A hunk header
    says how many old and new lines its hunk holds, so a line that begins like a file header is still a line. Raises
    Unreadable when the diff is not in a form this reader knows."""
    path, line, old_left, new_left = "", 0, 0, 0
    for text in diff.split("\n"):
        if old_left or new_left:
            if text.startswith("+"):
                yield path, line, text[1:]
                line, new_left = line + 1, new_left - 1
            elif text.startswith("-"):
                old_left -= 1
        elif text.startswith("+++ "):
            path = header_path(text[4:])
        elif text.startswith("@@"):
            header = HUNK_HEADER.match(text)
            if not header:
                raise Unreadable(f"git wrote the hunk header {text[:80]!r}, which the secrets guard does not know; the added lines were not screened")
            old_left, line, new_left = int(header.group(1) or 1), int(header.group(2)), int(header.group(3) or 1)
    if old_left or new_left:
        raise Unreadable(f"the diff ended inside a hunk, with {old_left} old and {new_left} new lines unread; the added lines were not screened")
    assert line >= 0, f"the diff gave the line number {line} for {path}, which is no line"
    assert path or line == 0, f"the diff added lines of a file it never named: {line} lines read"


def secret_lines(root: Path, skipped: set[str], unread: set[str] = frozenset()) -> list[Refusal]:
    """The first line of each file the index changes, apart from skipped, that adds a high-confidence secret, once for
    each kind of secret. Only the lines the edit adds are read, from one diff of the whole index, which leaves out the
    files in unread: they are too large to read. When the diff is not in a form this reader knows, every staged file
    that is not skipped or unread is refused as unscreened, since a secret may be in it."""
    exclusions = [f":(exclude,literal){path}" for path in sorted(unread)]
    diff = run_git(root, ["-c", "core.quotepath=false", "diff", "--cached", "--text", "-U0", "--no-color", "--no-ext-diff", "--no-textconv",
                          "--src-prefix=a/", "--dst-prefix=b/", "--no-renames", "--diff-filter=AMT", "--", ".", *exclusions])
    found: dict[tuple[str, str], int] = {}
    try:
        for path, line, text in added_lines(diff):
            hit = (CONTENT_PATTERN.search(text) or CONTENT_PATTERN.search(text.replace("\0", ""))) if path not in skipped else None
            if hit and hit.lastgroup:
                found.setdefault((path, CONTENT_RULES[int(hit.lastgroup.removeprefix("rule"))][0]), line)
    except Unreadable:
        return [Refusal(path, UNREADABLE_RULE) for _, _, path in staged_files(root) if path not in skipped and path not in unread]
    assert all(number > 0 for number in found.values()), f"a secret was found on line 0 of {found}"
    assert not {path for path, _ in found} & skipped, f"a skipped file was read for secrets: {found}"
    return [Refusal(path, rule, number) for (path, rule), number in found.items()]


def oversized_in_work_tree(root: Path) -> list[Refusal]:
    """The files that are changed or new in the work tree and larger than MAX_STAGED_BYTES, found from their sizes on disk
    before git reads any of them. A directory, a submodule and a file that vanished are no file of that size."""
    names = [name for name in run_git(root, ["ls-files", "--modified", "--others", "--exclude-standard", "-z"]).split("\0") if name]
    found = []
    for name in dict.fromkeys(names):
        try:
            status = os.lstat(root / name)
        except OSError:
            continue
        if stat.S_ISREG(status.st_mode) and status.st_size > MAX_STAGED_BYTES:
            found.append(Refusal(name, SIZE_RULE))
    assert all(refusal.rule == SIZE_RULE for refusal in found), f"{found} include a refusal that is no size refusal"
    assert len(found) <= len(set(names)), f"{len(found)} oversized files among {len(set(names))} changed ones"
    return found


def refusals(root: Path, allow: tuple[str, ...]) -> list[Refusal]:
    """Why the staged files in the repository at root must not be committed: a name that says secrets, a line that
    is a high-confidence secret, or a size over MAX_STAGED_BYTES. An allowlist entry skips the first two for the files
    it names, never the third."""
    files = staged_files(root)
    allowed = {path for _, _, path in files if is_allowed(path, allow)}
    named = [Refusal(path, NAME_RULE) for _, _, path in files if path not in allowed and name_rule(path)]
    large = oversized(root, files)
    contents = secret_lines(root, allowed | {refusal.path for refusal in named}, {refusal.path for refusal in large})
    found = [*named, *contents, *large]
    assert len({(refusal.path, refusal.rule, refusal.line) for refusal in found}) == len(found), f"a refusal is listed twice in {found}"
    assert not any(refusal.path in allowed and refusal.rule != SIZE_RULE for refusal in found), f"an allowed file is refused by name or content in {found}"
    return found


SCREENING_FAILURES = (CheckpointError, AssertionError, ValueError, LookupError, TypeError, OSError, subprocess.SubprocessError, RuntimeError)


def unstage(root: Path) -> None:
    """Takes everything out of the index, leaving the work tree as it is; a failure to do so is left for the commit to report."""
    assert root.is_dir(), f"{root} is not a directory to unstage in"
    with contextlib.suppress(CheckpointError):
        run_git(root, ["reset", "-q"])
    assert root.is_dir(), f"{root} went away while its index was emptied"


def checkpoint(cwd: Path, allow: tuple[str, ...] = ()) -> str | None:
    """Commits every file that is not ignored, new files included, and returns the new commit, or None when there
    was nothing to commit. The repository's own commit hooks and signing apply. When the commit fails, raises
    CheckpointError and leaves the changes staged, so no work is lost. Refuses while an operation is in progress or
    an untracked repository sits in the workspace. When a file is named like a secret, adds a high-confidence secret
    or is over MAX_STAGED_BYTES, and no entry of allow names it for the first two, raises Refused, commits nothing and
    unstages everything, so the work tree is as the edit left it. An allowlist with an entry that is empty or only
    wildcards raises ConfigurationError, as the hook does for the same file."""
    allow = validate_allow(allow)
    root = repository_root(cwd)
    refuse_unsafe(root, "checkpoint")
    if has_commit(root) and not on_branch(root):
        raise CheckpointError(f"no checkpoint: HEAD is detached in {root}, so the commit would belong to no branch; switch to a branch first")
    nested = nested_repositories(root)
    if nested:
        raise CheckpointError(f"no checkpoint: the untracked nested repository {', '.join(nested)} would be committed as an empty link; ignore or remove it")
    too_large = oversized_in_work_tree(root)
    if too_large:
        raise Refused(too_large)
    run_git(root, ["add", "-A"])
    try:
        files = [name for name in run_git(root, ["diff", "--cached", "--name-only", "-z"]).split("\0") if name]
        found = refusals(root, allow) if files else []
    except SCREENING_FAILURES as error:
        unstage(root)
        if isinstance(error, CheckpointError):
            raise
        raise CheckpointError(f"no checkpoint: screening the staged files failed with {type(error).__name__}: {error}; nothing was committed and the changes are unstaged") from error
    if not files:
        return None
    if found:
        run_git(root, ["reset", "-q"])
        raise Refused(found)
    before = run_git(root, ["rev-parse", "HEAD"]).strip() if has_commit(root) else ""
    try:
        run_git(root, ["commit", "-q", "-m", message(files)])
    except CheckpointError as error:
        raise CheckpointError(f"the checkpoint commit failed and its changes are left staged: {error}") from error
    head = run_git(root, ["rev-parse", "HEAD"]).strip()
    assert len(head) in (40, 64), f"HEAD {head!r} is neither a SHA-1 nor a SHA-256 object ID"
    assert head != before, f"the commit of {files} left HEAD at {before}; git reported success but made no commit"
    changed = uncommitted(root)
    if changed:
        raise CheckpointError(f"checkpoint {head} was made, but the commit hooks left {changed} changed and uncommitted; TCR cannot rely on a clean tree")
    return head


def snapshot_tree(root: Path, names: list[str]) -> str:
    """The tree of HEAD with the files in names as they are in the workspace now, made in a temporary index so that HEAD,
    the repository's index and the workspace stay as they are."""
    with tempfile.TemporaryDirectory() as directory:
        index, listing = Path(directory) / "index", Path(directory) / "paths"
        run_git(root, ["read-tree", "HEAD"], io=GitIO(index=index))
        if names:
            listing.write_bytes(b"\0".join(os.fsencode(name) for name in names))
            run_git(root, ["--literal-pathspecs", "add", "-A", "--pathspec-from-file", str(listing), "--pathspec-file-nul"], io=GitIO(index=index))
        tree = run_git(root, ["write-tree"], io=GitIO(index=index)).strip()
    assert len(tree) in (40, 64), f"git wrote the tree {tree!r}, which is neither a SHA-1 nor a SHA-256 object ID"
    assert names or tree == run_git(root, ["rev-parse", "HEAD^{tree}"]).strip(), f"a snapshot of no files is not the tree of HEAD in {root}"
    return tree


def identity_for_saved_commit(root: Path) -> tuple[tuple[str, str], ...]:
    """Nothing when git knows who the author and the committer are, as the person set them; otherwise FALLBACK_IDENTITY,
    so that a repository without a configured identity can still save the work a revert discards."""
    assert root.is_dir(), f"{root} is not a directory to ask git for an identity in"
    try:
        run_git(root, ["var", "GIT_AUTHOR_IDENT"])
        run_git(root, ["var", "GIT_COMMITTER_IDENT"])
    except CheckpointError:
        return FALLBACK_IDENTITY
    assert not FALLBACK_IDENTITY or FALLBACK_IDENTITY[0][0] == "GIT_AUTHOR_NAME", "the fallback identity does not start with the author's name"
    return ()


def point_ref_at(root: Path, ref: str, tree: str, *, is_new: bool) -> None:
    """Makes ref name a commit of tree on top of HEAD; a new ref must not exist yet."""
    commit = run_git(root, ["commit-tree", tree, "-p", "HEAD", "-m", SAVED_MESSAGE], io=GitIO(environment=identity_for_saved_commit(root))).strip()
    run_git(root, ["update-ref", ref, commit, *([""] if is_new else [])])
    assert run_git(root, ["rev-parse", ref]).strip() == commit, f"{ref} does not name the saved commit {commit}"
    assert run_git(root, ["rev-parse", f"{ref}^"]).strip() == run_git(root, ["rev-parse", "HEAD"]).strip(), f"{ref} is not on top of HEAD"


def save_discarded(root: Path, names: list[str], after_save: Callable[[], None] | None) -> tuple[str, list[str]]:
    """Saves the files in names, which a revert would discard, under a new ref below SAVED_REF_PREFIX, and returns the
    ref and the files that were saved. Anyone can write a file while that takes time, so after each save it lists the
    files again and saves again when they differ, until a list gives the tree already saved, MAX_SAVE_ATTEMPTS times at
    most; then it deletes the ref and raises CheckpointError, so that the caller deletes nothing. after_save runs after
    each save, before the list."""
    tree = snapshot_tree(root, names)
    ref = SAVED_REF_PREFIX + datetime.now(timezone.utc).strftime(SAVED_REF_TIME)
    point_ref_at(root, ref, tree, is_new=True)
    assert ref.startswith(SAVED_REF_PREFIX), f"the ref {ref} is not below {SAVED_REF_PREFIX}"
    try:
        for _ in range(MAX_SAVE_ATTEMPTS):
            if after_save is not None:
                after_save()
            names = uncommitted(root)
            later = snapshot_tree(root, names)
            if later == tree:
                return ref, names
            tree = later
            point_ref_at(root, ref, tree, is_new=False)
        raise CheckpointError(f"the files in {root} kept changing while the work was saved, so nothing was reverted; stop what writes to them and try again")
    except CheckpointError:
        run_git(root, ["update-ref", "-d", ref])
        assert not run_git(root, ["for-each-ref", ref]).strip(), f"the ref {ref} is still there after the save was given up"
        raise


def remove_untracked(root: Path, names: list[str]) -> None:
    """Removes the files in names that are neither tracked nor ignored, and the directories that held them and are empty
    now. A file that is not in names stays, even in the same directory, so that what was written after names was listed
    is not lost."""
    assert root.is_dir() and not any(name.startswith("/") for name in names), f"{names} are not paths below {root}"
    assert all(name for name in names), f"{names} include an empty path, which git clean would read as every file"
    for start in range(0, len(names), CLEAN_BATCH):
        run_git(root, ["--literal-pathspecs", "clean", "-fd", "--", *names[start:start + CLEAN_BATCH]])
    directories = sorted({parent for name in names for parent in (root / name).parents if parent != root and root in parent.parents}, key=lambda path: -len(path.parts))
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            continue


def revert(cwd: Path, *, save_ref: bool = True, after_save: Callable[[], None] | None = None, before_discard: Callable[[], None] | None = None) -> str | None:
    """Restores the latest commit: tracked files as committed, and files that are neither tracked nor ignored removed,
    with the directories that held only them. Ignored files, untracked nested repositories and the files inside
    submodules stay. Checks everything first and changes nothing when there is no commit to restore or an operation is in
    progress. Unless save_ref is false, the files it discards are first saved under a ref, which it returns, and saved
    again until a list of the files finds nothing new; when that cannot be done, or git fails while it saves, the ref
    is deleted and nothing is reverted. It removes only the untracked files of that last list, so a new file written
    after it stays and is reported by CheckpointError; a file of the list or a tracked file changed after it is lost,
    which can take tens of milliseconds, because git has no way to reset and list in one step. before_discard runs
    after the last list, before anything is discarded."""
    root = repository_root(cwd)
    if not has_commit(root):
        raise CheckpointError(f"no revert: {root} has no commit to restore; nothing was changed")
    refuse_unsafe(root, "revert")
    unwatched = unwatched_edits(root)
    if unwatched:
        raise CheckpointError(f"no revert: {UNWATCHED_REASON.format(files=named(unwatched))}; nothing was changed")
    run_git(root, ["clean", "-nfd"], warnings_fail=True)
    names = uncommitted(root)
    saved, discarded = save_discarded(root, names, after_save) if save_ref and names else (None, names)
    if before_discard is not None:
        before_discard()
    run_git(root, ["reset", "--hard", "HEAD"])
    remove_untracked(root, discarded)
    left = uncommitted(root)
    if left:
        raise CheckpointError(f"the revert in {root} left {left}; restore them by hand")
    assert not left, f"{root} still differs from HEAD in {left} after the revert"
    assert has_commit(root), f"{root} lost its commit during the revert"
    assert saved is None or save_ref, f"a revert saved {saved} without being asked to"
    return saved
