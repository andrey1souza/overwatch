"""Keep each sentence of the Markdown in git on a line of its own."""
import sys
import unittest

import paths
from markdown_format import format_markdown

assert str(paths.ROOT / "scripts") in sys.path, f"{paths.ROOT / 'scripts'} is not on sys.path {sys.path}; paths puts scripts/ there so markdown_format imports by name"


class MarkdownFormatTests(unittest.TestCase):
    def test_each_sentence_starts_a_line(self) -> None:
        self.assertEqual(format_markdown("One. Two! Three? Four."), "One.\nTwo!\nThree?\nFour.")

    def test_a_sentence_may_start_with_code(self) -> None:
        self.assertEqual(format_markdown("Run it. `Ack` replies."), "Run it.\n`Ack` replies.")

    def test_abbreviations_code_and_link_text_do_not_end_a_sentence(self) -> None:
        for line in ("Use a runner, e.g. Zig, first.", "Run `a. B` now.", "See [the spec. Here](x) now."):
            self.assertEqual(format_markdown(line), line)

    def test_fences_lists_quotes_and_tables_are_left_alone(self) -> None:
        text = "```\nOne. Two.\n```\n- One. Two.\n1. One. Two.\n> One. Two.\n| One. Two. |"
        self.assertEqual(format_markdown(text), text)

    def test_a_split_line_keeps_its_indent(self) -> None:
        self.assertEqual(format_markdown("  One. Two."), "  One.\n  Two.")

    def test_formatting_twice_changes_nothing(self) -> None:
        text = "One. Two.\n\n  Three. Four."
        self.assertEqual(format_markdown(format_markdown(text)), format_markdown(text))

    def test_frontmatter_is_left_alone(self) -> None:
        text = '---\n.description = "One. Two.",\n---\n\nOne. Two.'
        self.assertEqual(format_markdown(text), '---\n.description = "One. Two.",\n---\n\nOne.\nTwo.')


if __name__ == "__main__":
    unittest.main()
