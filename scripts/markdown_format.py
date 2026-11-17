#!/usr/bin/env python3
"""Put every sentence of the hand-written Markdown on a line of its own, or with --check list the files that need it.

With --stdin it formats stdin to stdout, so an editor can run it on save; with --check it rewrites nothing and
exits 1 when a file would change. Code spans, link text, fenced code, list items, quotes and tables are left alone.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
FILES = (ROOT / "README.md", ROOT / "spec/README.md")
ABBREVIATIONS = frozenset({
    "vs", "e.g", "i.e", "etc", "cf", "al", "no", "fig", "eq", "vol",
    "mr", "mrs", "ms", "dr", "st", "sr", "jr", "approx",
})
LIST_MARKER = re.compile(r"^\s*([-*+]|\d+[.)])\s")
FENCE = re.compile(r"^\s*(```+|~~~+)")


def split_sentences(line: str) -> str:
    """line with a line break after each sentence that another follows, each new line indented as line is."""
    indent = line[: len(line) - len(line.lstrip(" \t"))]
    result: list[str] = []
    depth = 0
    in_code = False
    i = 0
    while i < len(line):
        char = line[i]
        if char == "`":
            in_code = not in_code
        elif not in_code and char == "[":
            depth += 1
        elif not in_code and char == "]":
            depth = max(0, depth - 1)
        ends = char in ".!?" and depth == 0 and not in_code and line[i + 1 : i + 2] == " " and (line[i + 2 : i + 3].isupper() or line[i + 2 : i + 3] == "`")
        if ends:
            j = i - 1
            while j >= 0 and (line[j].isalnum() or line[j] == "."):
                j -= 1
            if not (char == "." and line[j + 1 : i].lower().rstrip(".") in ABBREVIATIONS):
                result.extend([char, "\n", indent])
                i += 2
                continue
        result.append(char)
        i += 1
    text = "".join(result)
    assert text.replace("\n" + indent, " ") == line, f"splitting {line!r} changed more than its line breaks"
    return text


def format_markdown(content: str) -> str:
    """content with each prose line split into one line per sentence, outside fences, lists, quotes and tables."""
    lines: list[str] = []
    fence: str | None = None
    for line in content.split("\n"):
        match = FENCE.match(line)
        if match:
            if fence is None:
                fence = match[1][0]
            elif line.strip().startswith(fence):
                fence = None
            lines.append(line)
        elif fence is not None or LIST_MARKER.match(line) or line.strip().startswith((">", "|")):
            lines.append(line)
        else:
            lines.append(split_sentences(line))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="List the files that would change, rewriting none")
    parser.add_argument("--stdin", action="store_true", help="Format stdin to stdout instead of the repository's files")
    args = parser.parse_args()
    if args.stdin:
        sys.stdout.write(format_markdown(sys.stdin.read()))
        return 0
    stale = [path for path in FILES if format_markdown(text := path.read_text()) != text]
    for path in stale:
        if args.check:
            print(f"{path.relative_to(ROOT)} has lines with more than one sentence; run python3 scripts/markdown_format.py", file=sys.stderr)
        else:
            path.write_text(format_markdown(path.read_text()))
            print(f"{path.relative_to(ROOT)} formatted")
    return 1 if stale and args.check else 0


if __name__ == "__main__":
    raise SystemExit(main())
