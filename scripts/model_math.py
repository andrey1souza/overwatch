"""Generate documentation equations from selected definitions in the authoritative TLA+ modules."""
from pathlib import Path
import re

# Only the reading order and explanations live here. Formula bodies are read from TLA+.
EQUATIONS = (
    ('FailurePolicy', 'NewFailure', 'A timeout or a finding outside the baseline counts as a new failure.'),
    ('FailurePolicy', 'FailureGuards', 'A nonempty finding set guards an action when inherited findings are blocked or the findings include a new failure.'),
    ('Overwatch', 'ReadsSelectLatestComplete', 'Reading buffered results selects the newest completed result.'),
    ('Overwatch', 'BaselineNeverAbsorbsEdits', 'The inherited baseline cannot absorb findings introduced by later edits.'),
    ('Overwatch', 'RunsEnd', 'A running check eventually ends. This is the progress assumption used by the host layer.'),
    ('Overwatch', 'RegressionsReachAgent', 'A regression eventually reaches the agent, or the check times out.'),
    ('IntegrationContract', 'SelectedChecks', 'An empty check selection means all checks; otherwise the configured selection applies.'),
    ('IntegrationContract', 'Guarded', 'Only selected checks whose findings satisfy the shared failure policy guard an action.'),
    ('IntegrationContract', 'CanRespond', 'Inspection and repair can receive a response during a check. Protected requests follow the host capability.'),
    ('IntegrationContract', 'InspectionAlwaysAllowed', 'An answered inspection or repair request is always allowed.'),
    ('IntegrationContract', 'PendingContextIsCurrent', 'Pending failure context must still describe a report that warrants notification.'),
    ('IntegrationContract', 'CorrectionRequestsAreDeduplicated', 'Report changes, new sessions, and completed work bound the number of fresh correction requests.'),
    ('IntegrationContract', 'FinishedReportsBecomeVisible', 'Once a check has finished, its report eventually becomes visible unless the session closes.'),
)
SYMBOLS = {
    '/\\': r'\land', '\\/': r'\lor', '~>': r'\leadsto', '=>': r'\Rightarrow',
    '==': r'\triangleq', '<=': r'\leq', '>=': r'\geq', '#': r'\neq', '~': r'\neg',
    '[]': r'\Box', '<>': r'\Diamond', '\\A': r'\forall', '\\E': r'\exists',
    '\\in': r'\in', '\\subseteq': r'\subseteq', '\\cup': r'\cup', '\\': r'\setminus',
    '{': r'\{', '}': r'\}', '<<': r'\langle', '>>': r'\rangle',
}
TOKEN = re.compile(r'\\[A-Za-z]+|' + '|'.join(re.escape(key) for key in sorted(SYMBOLS, key=len, reverse=True)) +
                   r'|"(?:\\.|[^"\\])*"|[A-Za-z][A-Za-z0-9_]*|[0-9]+|[()\[\],.:=+*/<>\x27]')
DEFINITION = re.compile(r'^([A-Za-z][A-Za-z0-9_]*(?:\([^\n)]*\))?)\s*==', re.MULTILINE)


def definition_text(source: str, name: str) -> str:
    """Extract exactly one complete definition, stopping at the next declaration or comment."""
    matches = list(DEFINITION.finditer(source))
    selected = [index for index, match in enumerate(matches) if match[1].split('(')[0] == name]
    assert len(selected) == 1, f'Expected one definition for {name}, found {len(selected)}'
    index = selected[0]
    end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
    text = re.split(r'^\(\*|^={4,}', source[matches[index].start():end], maxsplit=1, flags=re.MULTILINE)[0].strip()
    assert text.startswith(name), f'Extracted definition lost {name}: {text}'
    return text


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


def equation_latex(source: str, name: str) -> str:
    """Keep the definition's line structure and expose it as standard LaTeX math."""
    lines = definition_text(source, name).splitlines()
    lhs, _, first = lines[0].partition('==')
    body = [line.strip() for line in [first, *lines[1:]] if line.strip()]
    assert body, f'{name} has no defining expression'
    rows = [latex_line(lhs.strip()) + r' &\triangleq']
    rows.extend(r'&\quad ' + latex_line(line) for line in body)
    assert len(rows) == len(body) + 1, f'{name} lost a source line during typesetting'
    return '\\begin{aligned}\n' + ' \\\\\n'.join(rows) + '\n\\end{aligned}'


def model_equations(repo: Path) -> list[str]:
    """Interleave source-derived mathematics with the guide's explanations."""
    lines = ['## Model equations', '',
             'These equations are generated from the named TLA+ definitions. The notation is rendered as mathematics in this page; the complete modules remain available below.', '']
    assert len({(module, name) for module, name, _ in EQUATIONS}) == len(EQUATIONS), 'Equation selections must be unique'
    for module, name, explanation in EQUATIONS:
        source = (repo / 'spec' / f'{module}.tla').read_text()
        lines.extend([f'### {name}', '', explanation, '', '```=mathtex', *equation_latex(source, name).splitlines(), '```', '',
                      f'[Definition in {module}.tla](https://github.com/benomahony/overwatch/blob/main/spec/{module}.tla)', ''])
    assert lines.count('```=mathtex') == len(EQUATIONS), 'Every selected definition needs a rendered equation'
    return lines
