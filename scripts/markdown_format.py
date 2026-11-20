#!/usr/bin/env python3
"""Put every sentence of the Markdown in git on a line of its own, or with --check list the files that need it.

With --stdin it formats stdin to stdout, so an editor can run it on save; with --check it rewrites nothing and
exits 1 when a file would change. Frontmatter, code spans, link text, fenced code, list items, quotes and tables are
left alone.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ABBREVIATIONS = frozenset({
    "vs", "e.g", "i.e", "etc", "cf", "al", "no", "fig", "eq", "vol",
    "mr", "mrs", "ms", "dr", "st", "sr", "jr", "approx",
})
LIST_MARKER = re.compile(r"^\s*([-*+]|\d+[.)])\s")
FENCE = re.compile(r"^\s*(```+|~~~+)")


def ends_sentence(line: str, i: int) -> bool:
    """Whether the stop at line[i] ends a sentence: a space and then a capital or a code span follow it, and it is not
    the stop of an abbreviation."""
    assert line[i] in ".!?", f"ends_sentence was called at {line[i]!r} of {line!r}; call it at a stop"
    after = line[i + 2 : i + 3]
    if line[i + 1 : i + 2] != " " or not (after.isupper() or after == "`"):
        return False
    start = i - 1
    while start >= 0 and (line[start].isalnum() or line[start] == "."):
        start -= 1
    assert -1 <= start < i, f"the word before the stop at {i} of {line!r} starts at {start + 1}"
    return not (line[i] == "." and line[start + 1 : i].lower().rstrip(".") in ABBREVIATIONS)


def split_sentences(line: str) -> str:
    """line with a line break after each sentence that another follows, each new line indented as line is."""
    assert "\n" not in line, f"split_sentences was given {line!r}, which holds more than one line; pass one line"
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
        if char in ".!?" and depth == 0 and not in_code and ends_sentence(line, i):
            result.extend([char, "\n", indent])
            i += 2
            continue
        result.append(char)
        i += 1
    text = "".join(result)
    assert text.replace("\n" + indent, " ") == line, f"splitting {line!r} changed more than its line breaks"
    return text


def markdown_files() -> list[Path]:
    """Every Markdown and SuperMD file git tracks in the repository."""
    listed = subprocess.run(["git", "ls-files", "-z", "*.md", "*.smd"], cwd=ROOT, capture_output=True, text=True, check=True)
    files = [ROOT / name for name in listed.stdout.split("\0") if name]
    assert files, f"git lists no Markdown in {ROOT}; run from a checkout"
    assert all(path.suffix in (".md", ".smd") for path in files), f"git listed {[path.name for path in files if path.suffix not in ('.md', '.smd')]} as Markdown"
    return files


def format_markdown(content: str) -> str:
    """content with each prose line split into one line per sentence, outside frontmatter, fences, lists, quotes and
    tables."""
    lines: list[str] = []
    fence: str | None = None
    body = content.split("\n")
    if body[0] == "---" and "---" in body[1:]:
        end = body.index("---", 1) + 1
        lines, body = body[:end], body[end:]
    for line in body:
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
    assert len(lines) == len(content.split("\n")), f"formatting gave {len(lines)} lines for {len(content.split(chr(10)))}; each line maps to one"
    formatted = "\n".join(lines)
    assert formatted.count("\n") >= content.count("\n"), "formatting removed line breaks; it only adds them"
    return formatted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="List the files that would change, rewriting none")
    parser.add_argument("--stdin", action="store_true", help="Format stdin to stdout instead of the repository's files")
    args = parser.parse_args()
    if args.stdin:
        sys.stdout.write(format_markdown(sys.stdin.read()))
        return 0
    texts = {path: path.read_text() for path in markdown_files()}
    stale = [path for path, text in texts.items() if format_markdown(text) != text]
    assert all(path.is_relative_to(ROOT) for path in stale), f"{[str(path) for path in stale]} are not all under {ROOT}"
    for path in stale:
        if args.check:
            print(f"{path.relative_to(ROOT)} has lines with more than one sentence; run python3 scripts/markdown_format.py", file=sys.stderr)
        else:
            path.write_text(format_markdown(texts[path]))
            written = path.read_text()
            assert format_markdown(written) == written, f"formatting {path.relative_to(ROOT)} a second time changed it again"
            print(f"{path.relative_to(ROOT)} formatted")
    return 1 if stale and args.check else 0


if __name__ == "__main__":
    raise SystemExit(main())
