"""Check the generated site pages for what Zine refuses, so the Docs workflow cannot fail on it after the pages are written."""
import importlib.util
import unittest

import paths
from docs_pages import SECTION_ID, duplicate_ids, inline_html, page_texts


def load_sync():
    spec = importlib.util.spec_from_file_location("docs_sync", paths.ROOT / "docs" / "sync.py")
    assert spec is not None and spec.loader is not None, "docs/sync.py cannot be loaded as a module"
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    return sync


class SitePageTests(unittest.TestCase):
    def test_there_are_pages_to_scan(self) -> None:
        self.assertGreaterEqual(len(page_texts()), 8)

    def test_no_page_defines_a_section_id_twice(self) -> None:
        scanned = 0
        for name, text in page_texts().items():
            scanned += len(SECTION_ID.findall(text))
            self.assertEqual(duplicate_ids(text), [], f"{name} defines these section ids more than once; Zine refuses that")
        self.assertGreater(scanned, 100)

    def test_a_repeated_section_id_is_found(self) -> None:
        text = "## [A]($section.id('a'))\n\n## [B]($section.id('b'))\n\n## [A again]($section.id('a'))\n"
        self.assertEqual(duplicate_ids(text), ["a"])

    def test_no_page_has_inline_html_outside_code(self) -> None:
        scanned = 0
        for name, text in page_texts().items():
            scanned += len(text.splitlines())
            self.assertEqual(inline_html(text), [], f"{name} has HTML-like text outside code; Zine refuses it, so put it in a code span")
        self.assertGreater(scanned, 1000)

    def test_inline_html_is_found_outside_code_and_ignored_inside_it(self) -> None:
        self.assertEqual(inline_html('A point marked "reported by <name>; checked" rests.\n'), ["<name>"])
        self.assertEqual(inline_html("A point tagged `reported by <name>` rests.\n```\n<name>\n```\n"), [])

    def test_the_same_feature_in_two_groups_gets_two_ids(self) -> None:
        body = ["# [Verified]($heading.id('verified'))", "", "## [Mode]($heading.id('mode'))", "",
                "# [Faked]($heading.id('faked'))", "", "## [Mode]($heading.id('mode'))", ""]
        marked = load_sync().scenarios(body)
        self.assertEqual(duplicate_ids("\n".join(marked)), [])
        self.assertIn("$section.id('mode-faked')", marked[6])

    def test_an_id_that_the_group_cannot_separate_is_refused(self) -> None:
        body = ["# [Group]($heading.id('group'))", "", "## [Mode]($heading.id('mode'))", "", "## [Mode]($heading.id('mode'))", "",
                "## [Mode]($heading.id('mode'))", ""]
        with self.assertRaisesRegex(AssertionError, "on the page twice"):
            load_sync().scenarios(body)


if __name__ == "__main__":
    unittest.main()
