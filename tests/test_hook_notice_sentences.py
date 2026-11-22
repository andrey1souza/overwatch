"""Tests that tests/fixtures/notice-sentences.ts holds each fixed sentence of the notice builders of the plugin's hooks, which the test through the fake host puts in file names, check names and summaries: a notice that compares its own text with what a file name holds fails there. Run it with --write to write the fixture.

Copyright (c) 2026 benomahony. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import json
import re
import sys
import unittest

import paths
from codex_hook import read
from ts_source import calls, functions, arguments, scanned

HOOKS = paths.ROOT / "hooks"
REGISTRY = paths.ROOT / "tests" / "fixtures" / "hook-text-builders.ts"
FIXTURE = paths.ROOT / "tests" / "fixtures" / "notice-sentences.ts"
NOT_NOTICE_TEXT = ("assert", "new CommandError", "new Error")
LEAST_WORDS = 2
LEAST_CHARACTERS = 10
ESCAPES = {"n": "\n", "t": "\t"}
COMPARED_WITH_TEXT = ("includes", "startsWith", "endsWith")
SAID_ABOUT_NOTICES = ("The files are unchanged", "refs/x^2")


def unescaped(piece: str) -> str:
    return re.sub(r"\\(.)", lambda match: ESCAPES.get(match.group(1), match.group(1)), piece)


def inside_calls(code: str, names: tuple[str, ...]) -> list[tuple[int, int]]:
    spans = []
    for name in names:
        for opening in calls(code, name):
            spans.append((opening, arguments_end(code, opening)))
    return spans


def arguments_end(code: str, opening: int) -> int:
    spans = arguments(code, opening)
    return spans[-1][1] + 1 if spans else opening + 1


def pieces_of_the_file(file: str, names: set[str]) -> set[str]:
    source = read(HOOKS / file)
    code, spans = scanned(source)
    every = functions(code)
    builders = [each for each in every if each.name in names]
    skipped = inside_calls(code, NOT_NOTICE_TEXT)
    found = set()
    for first, last in spans:
        is_wanted = any(each.start <= first < each.end for each in builders) or not any(each.start <= first < each.end for each in every)
        piece = unescaped(source[first:last]).strip()
        if is_wanted and not any(a <= first < b for a, b in skipped) and len(piece) >= LEAST_CHARACTERS and len(piece.split()) >= LEAST_WORDS:
            found.add(piece)
    return found


def sentences_of_the_builders() -> list[str]:
    covered = json.loads(read(REGISTRY).split(" = ", 1)[1])["covered"]
    found = set(SAID_ABOUT_NOTICES)
    for file in sorted({key.split(":")[0] for key in covered}):
        found |= pieces_of_the_file(file, {key.split(":")[1] for key in covered if key.startswith(f"{file}:")})
    return sorted(found)


class NoticeSentenceTests(unittest.TestCase):
    def test_the_fixture_holds_each_fixed_sentence_of_the_notice_builders_and_the_sentences_the_review_named(self) -> None:
        held = json.loads(read(FIXTURE).split(" = ", 1)[1])
        self.assertEqual(held, sentences_of_the_builders(), "the fixture is not what the scan finds in the builders; run python3 tests/test_hook_notice_sentences.py --write")

    def test_the_scan_finds_a_sentence_of_each_notice_the_review_named_and_none_of_an_assert(self) -> None:
        found = sentences_of_the_builders()
        self.assertIn("TCR reverted part of the latest change", found)
        self.assertIn("The discarded work is gone: `tcr.saveReverted` is false in", found)
        self.assertFalse([each for each in found if "does not say it is about" in each], "a sentence of an assert message is in the fixture")


if __name__ == "__main__":
    if "--write" in sys.argv:
        FIXTURE.write_text(f"export const SENTENCES = {json.dumps(sentences_of_the_builders(), indent=2)}\n", encoding="utf-8")
    else:
        unittest.main()
