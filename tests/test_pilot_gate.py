"""Pilot acceptance gate over fake-CLI pilot arms: counts, evidence quality, verifier independence."""

from contextlib import redirect_stdout
import io
import unittest
from unittest import mock

import test_runner
from common import ReviewError, read_json


class PilotGateTests(unittest.TestCase):
    setUp = test_runner.RunnerTests.setUp
    scenario = test_runner.RunnerTests.scenario
    run_review = test_runner.RunnerTests.run_review

    def test_gate_passes_a_clean_pilot_and_reports_controls(self):
        from pilot_gate import evaluate, main
        positive, negative = 'src/route0.cpp', 'src/route5.cpp'
        self.scenario({'codex': {positive: {'candidates': [test_runner.finding(positive)]},
                                 'src/route1.cpp': {'candidates': [test_runner.finding('src/route1.cpp', defect_confidence=3)]},
                                 'gpt-6-astra:src/route2.cpp': {'candidates': [test_runner.finding('src/route2.cpp',
                                                                                                    kind='identity-typo')]}},
                       'claude': {'src/route2.cpp': {'status': 'rejected'}}})
        for mode in ('exhaustive', 'targeted'):
            self.assertEqual(self.run_review(mode), 0)
        runs = [self.base / 'exhaustive-run', self.base / 'targeted-run']
        report = evaluate(runs, positives=[positive], negatives=[negative], max_candidates=4, max_admitted_ratio=0.75)
        self.assertTrue(report['passed'], report)
        self.assertEqual(report['totals']['discovery_succeeded'], 24)
        self.assertEqual(report['totals']['discovery_failed'], 0)
        self.assertEqual(report['totals']['raw_findings'], 10)
        self.assertEqual(report['totals']['screened'], 4)
        self.assertEqual(report['totals']['candidate_count'], 4)
        self.assertEqual(report['totals']['verifier_succeeded'], 4)
        self.assertEqual(report['totals']['verifier_failed'], 0)
        self.assertEqual((report['totals']['confirmed'], report['totals']['rejected'], report['totals']['needs_validation']),
                         (2, 2, 0))
        self.assertTrue(all(check['pass'] for check in report['checks'].values()), report['checks'])
        self.assertEqual(report['controls'][positive]['confirmed'], 2)
        self.assertEqual(report['controls'][negative]['confirmed'], 0)
        for run in report['runs'].values():
            self.assertEqual(run['counts']['candidate_count'], 2)
            self.assertTrue(run['checks']['independence']['pass'])
        self.assertFalse(evaluate(runs, positives=[], negatives=[], max_candidates=1, max_admitted_ratio=0.75)['passed'])
        self.assertFalse(evaluate(runs, positives=[], negatives=[], max_candidates=4, max_admitted_ratio=0.1)['passed'])
        # The ratio is opt-in: the absolute per-run count is the mandatory candidate gate.
        unbounded = evaluate(runs, positives=[], negatives=[], max_candidates=4)
        self.assertTrue(unbounded['passed'], unbounded['checks'])
        self.assertIn('ratio=0.40', unbounded['checks']['candidate_count']['detail'])
        self.assertFalse(evaluate(runs, positives=[negative], negatives=[], max_candidates=4, max_admitted_ratio=0.75)['passed'])
        self.assertFalse(evaluate(runs, positives=[], negatives=[positive], max_candidates=4, max_admitted_ratio=0.75)['passed'])
        self.assertFalse(evaluate(runs, positives=[], negatives=[], max_candidates=4, max_admitted_ratio=0.75,
                                  max_cost_usd=0.01)['passed'])
        argv = ['pilot_gate.py', '--run', str(runs[0]), '--run', str(runs[1]), '--positive', positive,
                '--negative', negative, '--max-candidates', '4', '--max-admitted-ratio', '0.75',
                '--out', str(self.base / 'gate.json')]
        with mock.patch('sys.argv', argv), redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(), 0)
        self.assertIn('PASS', stdout.getvalue())
        self.assertTrue(read_json(self.base / 'gate.json')['passed'])
        with mock.patch('sys.argv', argv[:-2] + ['--max-candidates', '1']), redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(), 1)
        self.assertIn('FAIL', stdout.getvalue())

    def test_discovery_losses_need_an_explicit_allowance_and_unit_coverage(self):
        from pilot_gate import evaluate
        # One model loses one unit terminally; the other model still covers that unit.
        self.scenario({'codex': {'src/route0.cpp': {'candidates': [test_runner.finding('src/route0.cpp')]},
                                 'gpt-6-astra:src/route3.cpp': {'always': 'exit'}}})
        self.assertEqual(self.run_review(), 1)
        out = self.base / 'exhaustive-run'
        strict = evaluate([out], positives=[], negatives=[], max_candidates=4)
        self.assertFalse(strict['passed'])
        (run,) = strict['runs'].values()
        self.assertFalse(run['checks']['integrity']['pass'])
        self.assertIn('discovery_failed=1', run['checks']['integrity']['detail'])
        self.assertIn('uncovered_units=0', run['checks']['integrity']['detail'])
        self.assertEqual(run['counts']['discovery_failed'], 1)
        self.assertTrue(run['checks']['independence']['pass'])
        allowed = evaluate([out], positives=[], negatives=[], max_candidates=4, max_discovery_failures=1)
        self.assertTrue(allowed['passed'], allowed['checks'])
        self.assertEqual(allowed['runs'][str(out.resolve())]['state'], 'failed')
        # Both models lose the same unit: no allowance can cover a unit nobody reviewed.
        self.scenario({'codex': {'src/route3.cpp': {'always': 'exit'}}})
        self.assertEqual(self.run_review('targeted'), 1)
        uncovered = evaluate([self.base / 'targeted-run'], positives=[], negatives=[], max_candidates=4,
                             max_discovery_failures=2)
        self.assertFalse(uncovered['passed'])
        (run,) = uncovered['runs'].values()
        self.assertIn('uncovered_units=1', run['checks']['integrity']['detail'])

    def test_gate_refuses_changed_artifacts(self):
        from pilot_gate import evaluate
        self.scenario({'codex': {'src/route0.cpp': {'candidates': [test_runner.finding('src/route0.cpp')]}}})
        self.assertEqual(self.run_review(), 0)
        out = self.base / 'exhaustive-run'
        self.assertTrue(evaluate([out], positives=[], negatives=[], max_candidates=4)['passed'])
        prompt = next((out / 'results/verifier').glob('*/prompt.txt'))
        prompt.write_text(prompt.read_text() + '\nprior_candidates: []\n')
        with self.assertRaises(ReviewError):
            evaluate([out], positives=[], negatives=[], max_candidates=4)


if __name__ == '__main__':
    unittest.main()
