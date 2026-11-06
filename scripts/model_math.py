"""Render the complete TLA+ sources as documentation, retaining source order and organisation."""
from pathlib import Path
import re
from spec_sources import spec_sources

SYMBOLS = {
    '/\\': r'\land', '\\/': r'\lor', '~>': r'\leadsto', '=>': r'\Rightarrow',
    '==': r'\triangleq', '<=': r'\leq', '>=': r'\geq', '#': r'\neq', '~': r'\neg',
    '[]': r'\Box', '<>': r'\Diamond', '\\A': r'\forall', '\\E': r'\exists',
    '\\in': r'\in', '\\subseteq': r'\subseteq', '\\cup': r'\cup', '\\': r'\setminus',
    '|->': r'\mapsto', '->': r'\to', '..': r'\mathbin{\text{..}}', '\\notin': r'\notin', '_': '_',
    '{': r'\{', '}': r'\}', '<<': r'\langle', '>>': r'\rangle',
}
TOKEN = re.compile(r'\\[A-Za-z]+|' + '|'.join(re.escape(key) for key in sorted(SYMBOLS, key=len, reverse=True)) +
                   r'|"(?:\\.|[^"\\])*"|[A-Za-z][A-Za-z0-9_]*|[0-9]+|[()\[\],.:=+*/<>_\x27-]')
DEFINITION = re.compile(r'^([A-Za-z][A-Za-z0-9_]*(?:\([^\n)]*\))?)\s*==', re.MULTILINE)


DECLARATION = re.compile(r'^(EXTENDS|CONSTANTS|ASSUME|VARIABLES)\b')


def source_blocks(source: str) -> list[dict]:
    """Read source sections, comments, declarations, and every definition in source order."""
    lines, blocks, index = source.splitlines(), [], 0
    while index < len(lines):
        line = lines[index]
        if not line.strip() or re.match(r'^[-=]{4,}', line):
            index += 1
            continue
        start = index
        if line.startswith('(*'):
            while '*)' not in lines[index]:
                index += 1
            text = '\n'.join(lines[start:index + 1])[2:].rsplit('*)', 1)[0].strip()
            assert '(*' not in text, f'Nested documentation comments need explicit handling: {text}'
            kind, name = ('section', text.splitlines()[0][9:]) if text.startswith('Section: ') else ('comment', '')
            blocks.append(dict(kind=kind, name=name, text=text, line=start + 1))
            index += 1
            continue
        match = DEFINITION.match(line) or DECLARATION.match(line)
        if match is None:
            raise ValueError(f'Unrecognised top-level TLA+ source at line {index + 1}: {line!r}')
        kind = 'definition' if DEFINITION.match(line) else 'declaration'
        index += 1
        while index < len(lines) and not (DEFINITION.match(lines[index]) or DECLARATION.match(lines[index]) or
                                          lines[index].startswith('(*') or re.match(r'^={4,}', lines[index])):
            index += 1
        blocks.append(dict(kind=kind, name=match[1].split('(')[0], text='\n'.join(lines[start:index]).strip(), line=start + 1))
    assert blocks, 'A specification module must contain declarations or definitions'
    return blocks


def definition_text(source: str, name: str) -> str:
    """Look up a definition in the complete source inventory used to render the docs."""
    found = [block['text'] for block in source_blocks(source) if block['kind'] == 'definition' and block['name'] == name]
    assert len(found) == 1, f'Expected one definition for {name}, found {len(found)}'
    assert found[0].startswith(name), f'Extracted definition lost {name}: {found[0]}'
    return found[0]


def latex_line(line: str) -> str:
    """Translate notation token by token; reject unsupported syntax rather than paraphrasing it."""
    tokens = list(TOKEN.finditer(line))
    previous, output = 0, []
    for match in tokens:
        if line[previous:match.start()].strip():
            raise ValueError(f'Unsupported TLA+ notation {line[previous:match.start()]!r} in {line!r}')
        token = match[0]
        if token in SYMBOLS:
            output.append(SYMBOLS[token])
        elif token.startswith('\\'):
            raise ValueError(f'Unsupported TLA+ command {token!r} in {line!r}')
        elif token.startswith('"'):
            output.append(r'\texttt{' + token.replace('_', r'\_') + '}')
        elif token[0].isalpha():
            output.append(r'\mathit{' + token.replace('_', r'\_') + '}')
        else:
            output.append(token)
        previous = match.end()
    assert not line[previous:].strip(), f'Unsupported TLA+ suffix in {line!r}: {line[previous:]!r}'
    assert output, f'Cannot typeset an empty expression: {line!r}'
    return ' '.join(output)


def block_latex(text: str) -> str:
    """Render a source block without omitting declarations, guards, or frame conditions."""
    lines = text.splitlines()
    lhs, separator, first = lines[0].partition('==')
    body = [line for line in ([first, *lines[1:]] if separator else lines) if line.strip()]
    assert body, f'Empty mathematical source block: {text!r}'
    first = r'&' + (latex_line(lhs.strip()) + r' \triangleq ' if separator else '') + latex_line(body[0].strip())
    rows = [first]
    rows.extend(r'&\quad ' + r'\hspace{' + str(round((len(line) - len(line.lstrip())) / 3, 2)) + 'em}' + latex_line(line.strip()) for line in body[1:])
    assert len(rows) == len(body), f'A source line was lost while typesetting {text!r}'
    return '\\begin{aligned}\n' + ' \\\\\n'.join(rows) + '\n\\end{aligned}'


def specification_blocks(repo: Path) -> list[tuple[str, list[dict]]]:
    """Discover all modules; there is no separate documentation definition catalog."""
    modules = [(source.stem, source_blocks(source.read_text())) for source in spec_sources(repo)]
    assert modules, f'No specification modules in {repo / "spec"}'
    assert len({name for name, _ in modules}) == len(modules), f'Duplicate modules: {modules}'
    return modules


def model_formulas(repo: Path) -> list[str]:
    """The complete mathematical inventory, shared by generation and HTML validation."""
    modules = specification_blocks(repo)
    assert all(any(block['kind'] in {'definition', 'declaration'} for block in blocks) for _, blocks in modules), f'A module has no mathematical content: {modules}'
    formulas = [block_latex(block['text']) for _, blocks in modules for block in blocks if block['kind'] in {'definition', 'declaration'}]
    assert all(formula.strip() for formula in formulas), f'Empty rendered expressions in {formulas}'
    return formulas


def model_equations(repo: Path) -> list[str]:
    """Publish the specification's own sections and comments in its own order."""
    lines = ["# Overwatch's behavioral specification", '',
             'Generated from every module in `spec/`. Sections, comments, declarations, and definitions follow the source.', '']
    for module, blocks in specification_blocks(repo):
        lines.extend([f'## {module}', '', f'[Source](https://github.com/benomahony/overwatch/blob/main/spec/{module}.tla)', ''])
        for block in blocks:
            text = block['text']
            if block['kind'] == 'section':
                lines.extend([f"### {module} - {block['name']}", '', *text.splitlines()[1:], ''])
            elif block['kind'] == 'comment':
                lines.extend([text.replace('\n', ' '), ''])
            else:
                lines.extend([f"#### {module} - {block['name']}", '', '```=mathtex',
                              *block_latex(text).splitlines(), '```', ''])
    assert sum(line.startswith('## ') for line in lines) == len(specification_blocks(repo)), 'A specification module was omitted from the documentation'
    assert lines.count('```=mathtex') == len(model_formulas(repo)), 'Source blocks were lost during documentation generation'
    return lines
