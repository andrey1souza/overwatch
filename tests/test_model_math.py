"""Ensure documentation mathematics follows the model and never silently drops notation."""
import unittest
from paths import ROOT
from model_math import EQUATIONS, definition_text, equation_latex, latex_line, model_equations


class ModelEquationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = {module: (ROOT / 'spec' / f'{module}.tla').read_text() for module, _, _ in EQUATIONS}

    def test_every_selected_definition_generates_one_expression(self) -> None:
        guide = model_equations(ROOT)
        self.assertEqual(guide.count('```=mathtex'), len(EQUATIONS))
        for module, name, _ in EQUATIONS:
            source = self.sources[module]
            self.assertTrue(definition_text(source, name).startswith(name))
            self.assertIn('\\triangleq', equation_latex(source, name))

    def test_model_change_changes_the_equation(self) -> None:
        source = self.sources['FailurePolicy']
        changed = source.replace('"timeout"', '"different-finding"')
        self.assertNotEqual(equation_latex(source, 'NewFailure'), equation_latex(changed, 'NewFailure'))
        self.assertIn('different-finding', equation_latex(changed, 'NewFailure'))

    def test_temporal_and_set_operators_preserve_their_distinctions(self) -> None:
        expression = latex_line(r'[]<>(a \ b) \subseteq c ~> ~busy')
        for operator in (r'\Box', r'\Diamond', r'\setminus', r'\subseteq', r'\leadsto', r'\neg'):
            self.assertIn(operator, expression)

    def test_unknown_notation_fails_instead_of_becoming_a_different_formula(self) -> None:
        with self.assertRaisesRegex(ValueError, 'Unsupported TLA'):
            latex_line(r'a @@ b')
        with self.assertRaisesRegex(ValueError, 'Unsupported TLA'):
            latex_line(r'a \unknown b')

    def test_definition_does_not_capture_the_next_definition(self) -> None:
        source = 'First(x) ==\n    x = 1\n\nSecond == FALSE\n===='
        self.assertNotIn('Second', definition_text(source, 'First'))
        with self.assertRaisesRegex(AssertionError, 'Expected one definition'):
            definition_text(source, 'Missing')


if __name__ == '__main__':
    unittest.main()
