"""Runs engine/engine.py in a real process whose event handling fails with a given error, for tests of what the entry point does with a bug.

Copyright (c) 2026 benomahony. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import os
import subprocess
import sys

from paths import ROOT

PROCESS_SECONDS = 60
PREVIEW_CHARACTERS = 40


def run_engine_that_fails_with(error: Exception, event: str) -> subprocess.CompletedProcess[str]:
    """The finished process of engine.main given event on stdin, with process_event replaced by one that raises error."""
    assert type(error).__name__ in repr(error), f"{error!r} does not name its type, so the process could not raise it again; pass a plain exception"
    assert event.startswith("{"), f"event {event[:PREVIEW_CHARACTERS]!r} is not a JSON object; the engine reads one from stdin"
    script = f"import engine\ndef broken(*arguments, **options):\n    raise {error!r}\nengine.process_event = broken\nengine.main()"
    finished = subprocess.run([sys.executable, "-c", script], input=event, capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": str(ROOT / "engine")}, timeout=PROCESS_SECONDS, check=False)
    assert isinstance(finished.returncode, int), f"the engine process ended with {finished.returncode!r}, not an exit code"
    return finished
