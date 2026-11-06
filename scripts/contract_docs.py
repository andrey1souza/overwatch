"""Render browsable scenarios directly from TLC's host-contract fixtures."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'https://github.com/benomahony/overwatch/blob/main'
HOST_NAMES = {'claude': 'Claude Code', 'codex': 'Codex', 'opencode1': 'OpenCode 1',
              'opencode2': 'OpenCode 2', 'pi': 'Pi', 'neovim': 'Neovim'}


def observation(state: dict) -> str:
    """Describe exported observations without recomputing any model decision."""
    assert state['ready'] or state['decision'] in {'none', 'cancelled'}, f'Unready reply has a decision: {state}'
    assert all(state[name] >= 0 for name in ('contexts', 'warnings', 'corrections', 'wakes')), f'Negative delivery count: {state}'
    reply = state['decision'] if state['ready'] else 'pending'
    findings = ', '.join(state['failures']) or 'none'
    parts = [f"reply `{reply}`", f"session {'active' if state['active'] else 'closed'}",
             f"check {'running' if state['busy'] else 'idle'}",
             f"visible report {'known' if state['visible'] else 'cleared'} (failing checks: {findings})",
             f"context {'pending' if state['pending'] else 'not pending'}"]
    parts.extend(f"{name}: {state[name]}" for name in ('contexts', 'warnings', 'corrections', 'wakes'))
    return '; '.join(parts) + '.'


def render_contracts(rows: list[dict]) -> list[str]:
    """Use fixture inputs and TLC expectations for every step, grouped by host."""
    assert rows and {row['host'] for row in rows} == set(HOST_NAMES), f'Documentation has {len(rows)} cases for {set(row["host"] for row in rows)}, expected {set(HOST_NAMES)}'
    assert len({(row['host'], row['name']) for row in rows}) == len(rows), 'Each contract needs a unique title'
    lines = ['## Model-derived integration contracts', '',
             f"These {len(rows)} cases are generated from the same TLC expectations replayed against all six integrations. "
             'Each event is followed by the model observation asserted by the replay. '
             'Counters track failure deliveries, warnings, corrections, and idle wakeups; they exclude routine logs and recovery notices.', '',
             '[Read the formal model guide](https://benomahony.github.io/overwatch/model/) for event definitions, host differences, and verification limits.', '']
    for host in dict.fromkeys(row['host'] for row in rows):
        lines.extend([f'### {HOST_NAMES[host]}', ''])
        for row in (row for row in rows if row['host'] == host):
            lines.extend(render_contract(row))
    return lines


def render_contract(row: dict) -> list[str]:
    """One card, with the exact initial state and each exported observation."""
    host = row['host']
    assert row['events'], f'Contract has no events: {row["name"]}'
    assert host in HOST_NAMES, f'Unknown host: {host}'
    lines = []
    states = row['expected']
    if len(states) != len(row['events']) + 1:
        raise ValueError(f"{host}: {row['name']} has missing TLC observations; regenerate the fixtures")
    title = row['name'].replace('[', '').replace(']', '').replace("'", '')
    initial = states[0]
    lines.extend([f"#### {HOST_NAMES[host]} - {title}", '',
                  f"Tags: model-derived, {host}, {row['inherited']}, {initial['delivery']}.", '',
                  f'[Executable replay]({SOURCE}/' + ('tests/contracts.test.ts' if host == 'claude' else
                  'tests/integration_contract_spec.lua' if host == 'neovim' else 'tests/test_integration_contract.mjs') + ')', '',
                  f"- **Given** inherited policy `{row['inherited']}`, selected checks `{json.dumps(row['selected'])}` "
                  f"(empty means all), reports `{json.dumps(row['reports'], sort_keys=True)}`, baseline `{json.dumps(row['baseline'], sort_keys=True)}`, "
                  f"and a turn {'in progress' if row['inTurn'] else 'idle'}.",
                  '- **And** ' + observation(initial)])
    for event, expected in zip(row['events'], states[1:], strict=True):
        detail = f" with result `{json.dumps(row['next'], sort_keys=True)}`" if event in ('Finish', 'LateFinish') else ''
        lines.extend([f'- **When** `{event}` occurs{detail}.', '- **Then** ' + observation(expected)])
    lines.append('')
    return lines


def render_capabilities(rows: list[dict]) -> list[str]:
    """Show capabilities exported by TLC, rejecting inconsistent host snapshots."""
    fields = ('agent', 'waitsForCheck', 'completion', 'delivery', 'persistentStatus', 'idleWake')
    hosts = {}
    for row in rows:
        for state in row['expected']:
            values = tuple(state[key] for key in fields)
            previous = hosts.setdefault(row['host'], values)
            assert previous == values, f"Capabilities change within {row['host']}: {previous} / {values}"
    assert set(hosts) == set(HOST_NAMES), f'Capabilities missing hosts: {set(HOST_NAMES) - set(hosts)}'
    lines = ['| Host | Agent gates | Waits for check on protected calls | Completion feedback | Delivery | Persistent status | Idle wake |',
             '| --- | --- | --- | --- | --- | --- | --- |']
    for host, values in hosts.items():
        cells = [('Yes' if value else 'No') if isinstance(value, bool) else f'`{value}`' for value in values]
        lines.append('| ' + ' | '.join([HOST_NAMES[host], *cells]) + ' |')
    return lines
