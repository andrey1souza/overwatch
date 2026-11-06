"""Require every generated LaTeX page to be visibly embedded in the built documentation."""
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ModelImages(HTMLParser):
    """Collect rendered model images and the link enclosing each image."""
    def __init__(self) -> None:
        super().__init__()
        self.images = []
        self.link = None
        assert not self.images, 'An HTML inspection must start without observations'
        assert self.link is None, 'An HTML inspection cannot start inside a link'

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        assert tag, f'HTML parser gave an empty tag: {attrs}'
        assert len(values) == len(attrs), f'Duplicate attributes on {tag}: {attrs}'
        if tag == 'a':
            self.link = values.get('href')
        if tag == 'img' and '/models/' in values.get('src', ''):
            self.images.append((values, self.link))

    def handle_endtag(self, tag: str) -> None:
        assert tag, 'HTML parser gave an empty closing tag'
        assert isinstance(self.images, list), f'Model image observations became {type(self.images).__name__}; expected list'
        if tag == 'a':
            self.link = None


def check_model_html() -> None:
    """Compare actual HTML images to generated assets and the complete module inventory."""
    assets = ROOT / 'docs/assets/models'
    expected = {path.name for path in assets.glob('*.svg')}
    modules = {path.stem for path in (ROOT / 'spec').glob('*.tla')}
    assert expected, 'No model renderings found; run python3 scripts/spec_latex.py --check'
    assert {name.rsplit('-', 1)[0] for name in expected} == modules, f'Inline model assets do not cover {modules}: {expected}'
    parser = ModelImages()
    parser.feed((ROOT / 'docs/public/model/index.html').read_text())
    observed = []
    for image, link in parser.images:
        name = image['src'].rsplit('/', 1)[-1]
        observed.append(name)
        assert image.get('alt'), f'Model rendering has no accessible description: {image}'
        assert link == image['src'], f'Model rendering cannot open at full size: {image} / {link}'
        assert (ROOT / 'docs/public/models' / name).is_file(), f'Model asset was not deployed: {name}'
    assert len(observed) == len(expected) and set(observed) == expected, f'Expected {expected} visible in the HTML, found {observed}'
    print(f'{len(observed)} inline LaTeX pages verified in built documentation')


if __name__ == '__main__':
    check_model_html()
