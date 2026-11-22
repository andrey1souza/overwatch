"""Tests that no `replace` or `replaceAll` call of the hooks gives its replacement as text that holds data.

`String.replace` reads `$&`, `$1`, a backtick and a quote after `$` in a replacement text, so a file or check name that holds `$` changes the sentence it is put into. The scan reads the source with the tokenizer of ts_source.py and checks the second argument of each call: it is a function, or a literal text, or a constant written in capitals. Anything else fails, so data goes in through a function.

Copyright (c) 2026 benomahony. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import re
import unittest

import paths
from codex_hook import read
from ts_source import arguments, calls, masked

HOOKS = paths.ROOT / "hooks"
METHODS = ("replace", "replaceAll")
FUNCTION = re.compile(r"^(?:async\s+)?(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>)")
CONSTANT = re.compile(r"^[A-Z][A-Z0-9_]*$")
LITERAL = re.compile(r"^(?:'[^']*'|\"[^\"]*\")$")


def is_safe(argument: str) -> bool:
    """Whether a replacement argument carries no data: a function, a quoted literal, or a constant."""
    text = argument.strip()
    return bool(FUNCTION.match(text) or CONSTANT.match(text) or LITERAL.match(text))


def replacements(source: str) -> list[str]:
    """The second argument of each `.replace(` and `.replaceAll(` call of source, as written."""
    code = masked(source)
    found: list[str] = []
    for method in METHODS:
        for opening in calls(code, method):
            spans = arguments(code, opening)
            if len(spans) >= 2 and code[opening - len(method) - 1] == ".":
                found.append(source[spans[1][0]:spans[1][1]])
    return found


class ReplacementTests(unittest.TestCase):
    def test_no_replacement_of_the_hooks_holds_data(self) -> None:
        unsafe = {path.name: [each.strip() for each in replacements(read(path)) if not is_safe(each)] for path in sorted(HOOKS.glob("*.ts"))}
        self.assertEqual({name: found for name, found in unsafe.items() if found}, {}, "a replacement holds data that `$` can change; give a function")

    def test_the_scan_finds_the_call_it_must_reject(self) -> None:
        found = replacements("const text = SENTENCE.replace('{names}', listed(skipped))")
        self.assertEqual([each.strip() for each in found if not is_safe(each)], ["listed(skipped)"])

    def test_the_scan_accepts_a_function_a_literal_and_a_constant(self) -> None:
        found = replacements("a.replace(X, () => listed(s)); b.replace(Y, 'x'); c.replace(Z, QUOTE); d.replace(W, (m, g) => g)")
        self.assertEqual([each for each in found if not is_safe(each)], [])

    def test_the_scan_counts_every_call_of_the_hooks(self) -> None:
        total = sum(len(replacements(read(path))) for path in HOOKS.glob("*.ts"))
        self.assertGreaterEqual(total, 10)


if __name__ == "__main__":
    unittest.main()
