"""The specification package declares one module order for reading and typesetting."""
import json
from pathlib import Path


def spec_sources(repo: Path) -> list[Path]:
    """Resolve the declared order against the complete module inventory, rejecting omissions."""
    directory = repo / 'spec'
    order = json.loads((directory / 'modules.json').read_text())
    available = {path.stem: path for path in directory.glob('*.tla')}
    assert isinstance(order, list) and all(isinstance(name, str) for name in order), f'Invalid module order: {order}'
    assert len(order) == len(set(order)), f'Duplicate modules in reading order: {order}'
    assert set(order) == set(available), f'Module order {order} differs from the specification inventory {sorted(available)}'
    return [available[name] for name in order]
