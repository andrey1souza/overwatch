"""Require source-derived equations to appear as native mathematics in the built documentation."""
from html.parser import HTMLParser
from pathlib import Path
from model_math import model_formulas
from spec_sources import spec_sources
import re

ROOT = Path(__file__).resolve().parents[1]


class ModelMath(HTMLParser):
    """Read TeX annotations from MathML and detect obsolete embedded document pages."""
    def __init__(self) -> None:
        super().__init__()
        self.formulas = []
        self.annotation = None
        self.math_count = 0
        self.visible_count = 0
        self.embeds = []
        self.headings = []
        assert not self.formulas, 'HTML inspection must start without observations'
        assert self.annotation is None, 'HTML inspection cannot start inside an annotation'

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        assert tag, f'Empty HTML tag with attributes {attrs}'
        assert len(values) == len(attrs), f'Duplicate attributes on {tag}: {attrs}'
        if tag == 'h2':
            self.headings.append(values.get('id'))
        if tag == 'span' and 'katex-html' in values.get('class', '').split():
            self.visible_count += 1
        if tag == 'math':
            self.math_count += 1
        if tag == 'annotation' and values.get('encoding') == 'application/x-tex':
            self.annotation = ''
        if tag in {'iframe', 'embed', 'object'} or tag == 'img' and '/models/' in values.get('src', ''):
            self.embeds.append(tag)

    def handle_data(self, data: str) -> None:
        assert '\x00' not in data, 'HTML data contains a null character'
        assert self.annotation is None or isinstance(self.annotation, str), f'Invalid annotation: {self.annotation}'
        if self.annotation is not None:
            before = self.annotation
            self.annotation += data
            assert self.annotation == before + data, 'TeX annotation content was lost'

    def handle_endtag(self, tag: str) -> None:
        assert tag, 'HTML parser gave an empty closing tag'
        assert isinstance(self.formulas, list), f'Equation observations became {type(self.formulas)}'
        if tag == 'annotation' and self.annotation is not None:
            self.formulas.append(self.annotation.strip())
            self.annotation = None


def check_model_html() -> None:
    """Compare the actual rendered equations with the current TLA+ definitions."""
    parser = ModelMath()
    html = (ROOT / 'docs/public/model/index.html').read_text()
    parser.feed(html)
    expected = model_formulas(ROOT)
    modules = [source.stem.lower() for source in spec_sources(ROOT)]
    assert parser.headings[:len(modules)] == modules, f'Document module order differs from the spec package: {parser.headings} / {modules}'
    css = ROOT / 'docs/public/vendor/katex.min.css'
    assert css.is_file(), f'Missing equation stylesheet: {css}'
    fonts = re.findall(r'url\((fonts/[^)]+)\)', css.read_text())
    assert fonts, 'Equation stylesheet contains no font references'
    assert all((css.parent / font).is_file() for font in fonts), f'Missing deployed equation fonts: {fonts}'
    assert parser.formulas == expected, f'Rendered equations differ from the TLA+ source: {len(parser.formulas)} observed, {len(expected)} expected'
    assert parser.math_count == len(expected), f'Expected {len(expected)} native math elements, found {parser.math_count}'
    assert parser.visible_count == len(expected), f'Expected {len(expected)} visible equation layouts, found {parser.visible_count}'
    assert not parser.embeds, f'Document pages are still embedded: {parser.embeds}'
    assert 'type="math/tex' not in html, 'TeX expressions were left unrendered'
    print(f'{len(expected)} source-derived equations, visible layouts, module order, and font assets verified')


if __name__ == '__main__':
    check_model_html()
