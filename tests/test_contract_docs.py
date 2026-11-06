"""Keep published behavior tied to TLC observations rather than copied expectations."""
from copy import deepcopy
import json
import unittest

from paths import ROOT
from contract_docs import HOST_NAMES, render_contract, render_contracts


class ContractDocumentationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = json.loads((ROOT / 'tests/fixtures/integration-contract.json').read_text())

    def test_every_fixture_and_event_is_published(self) -> None:
        text = '\n'.join(render_contracts(self.rows))
        self.assertEqual(text.count('#### '), len(self.rows))
        for row in self.rows:
            card = '\n'.join(render_contract(row))
            for event in row['events']:
                self.assertIn(f'`{event}` occurs', card)
            self.assertEqual(card.count('- **Then** '), len(row['events']))
            self.assertIn(HOST_NAMES[row['host']], card)

    def test_changed_model_observations_change_the_document(self) -> None:
        row = deepcopy(self.rows[0])
        original = render_contract(row)
        row['expected'][-1].update(ready=True, decision='warn', contexts=91, wakes=27)
        changed = '\n'.join(render_contract(row))
        self.assertNotEqual(original, changed.splitlines())
        self.assertIn('reply `warn`', changed)
        self.assertIn('contexts: 91', changed)
        self.assertIn('wakes: 27', changed)

    def test_incomplete_trace_cannot_be_published(self) -> None:
        row = deepcopy(self.rows[0])
        row['expected'].pop()
        with self.assertRaisesRegex(ValueError, 'missing TLC observations'):
            render_contract(row)



if __name__ == '__main__':
    unittest.main()
