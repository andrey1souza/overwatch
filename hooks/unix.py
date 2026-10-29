"""The POSIX calls overwatch's Codex hook makes, kept here so the rest of the hook is platform-neutral: an exclusive
file lock, waiting on a pipe for a command's output, and whether the current user owns a file.

Copyright (c) 2026 benomahony. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import fcntl
import os
import selectors
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

# How long one read of a command's output waits for more, in seconds, and the most it reads at once, in bytes.
READ_WAIT = 0.1
READ_CHUNK = 65_536


@contextmanager
def exclusive(handle: IO[str]) -> Iterator[None]:
    """Holds an exclusive flock on the open file handle for the block, released however the block ends. The caller
    opens the lock file and keeps it afterwards: removing it while another process waits on it would let a third
    lock a new file at the same path."""
    assert not handle.closed, f"lock {handle.name} is closed; open it before taking the lock"
    fcntl.flock(handle, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
    assert not handle.closed, f"lock {handle.name} closed inside the block; leave closing it to the caller"


def output_selector(stdout: IO[bytes]) -> selectors.BaseSelector:
    """A selector that read_available waits on for a command's output; on POSIX a pipe can be selected on."""
    selector = selectors.DefaultSelector()
    key = selector.register(stdout, selectors.EVENT_READ)
    assert key.fileobj is stdout, f"selector registered {key.fileobj}, not the command's output; register stdout"
    assert key.events == selectors.EVENT_READ, f"selector waits for events {key.events}; wait only to read"
    return selector


def read_available(selector: selectors.BaseSelector) -> bytes:
    """Whatever the registered output holds within READ_WAIT seconds, up to READ_CHUNK bytes; nothing when it holds none."""
    selected = selector.select(timeout=READ_WAIT)
    if not selected:
        return b""
    key, _ = selected[0]
    assert len(selected) == 1, f"selector watched {len(selected)} outputs; register only the command's output"
    chunk = os.read(key.fd, READ_CHUNK)
    assert len(chunk) <= READ_CHUNK, f"read {len(chunk)} bytes; read at most READ_CHUNK at once"
    return chunk


def is_owned(path: Path) -> bool:
    """Whether the current user owns the existing file or directory at path."""
    owner, user = path.stat().st_uid, os.getuid()
    assert owner >= 0, f"{path} has owner id {owner}, which no user has"
    assert user >= 0, f"the current user has id {user}, which no user has"
    return owner == user
