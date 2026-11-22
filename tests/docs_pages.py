"""Read the generated site pages and find what Zine refuses in them."""
import re

import paths

SECTION_ID = re.compile(r"\$section\.id\('([^']*)'\)")
CODE_SPAN = re.compile(r"`[^`\n]*`")
INLINE_HTML = re.compile(r"</?[A-Za-z][^<>\n]*>")
FENCE = "```"


def page_texts() -> dict[str, str]:
    """The text of each page in docs/content by file name."""
    pages = {page.name: page.read_text() for page in sorted((paths.ROOT / "docs" / "content").glob("*.smd"))}
    assert pages and all(pages.values()), f"docs/content has pages {sorted(pages)}, some empty; run python3 docs/sync.py --write"
    return pages


def duplicate_ids(text: str) -> list[str]:
    """The section ids that a page defines more than once."""
    ids = SECTION_ID.findall(text)
    return sorted({identifier for identifier in ids if ids.count(identifier) > 1})


def inline_html(text: str) -> list[str]:
    """The HTML-like tokens of a page outside its code blocks and code spans."""
    found: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.startswith(FENCE):
            fenced = not fenced
        elif not fenced:
            found.extend(INLINE_HTML.findall(CODE_SPAN.sub("", line)))
    return found
