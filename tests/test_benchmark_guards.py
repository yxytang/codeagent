"""Budget admission, incomplete observations and evaluator-owned process cleanup."""
from copy import deepcopy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from benchmarks.__main__ import main
from benchmarks.evaluator import evaluate, write_files
from benchmarks.runner import BenchmarkConfig, run_suite, self_check
from benchmarks.task_suite import TaskCase


class _Thread:
    def __init__(self, alive=False):
        self.alive = alive

    def is_alive(self):
        return self.alive

    def join(self, timeout):
        if self.alive:
            time.sleep(0.005)


class BenchmarkGuardTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / '.test-tmp'
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='benchmark-guards-', dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case = TaskCase('guard', 'bug_fix', 'fix module', {'module.py': 'x = 0\n'},
                             {'module.py': 'x = 1\n'}, 'raise SystemExit(0)\n')
        self.connection = {'base_url': 'https://example.invalid', 'model': 'fake', 'api_key': 'fixture-only-key'}

    def engine(self, *, usage_complete=True, alive=False):
        instances = []
        case = self.case
        class FakeEngine:
            def __init__(self, data_dir, project_dir, settings, runtime_options):
                self.data_dir = data_dir
                self.options = runtime_options
                self.threads = {'cell': _Thread(alive)}
                instances.append(self)

            def create(self, objective, workspace, **kwargs):
                if not alive:
                    write_files(self.data_dir / 'runs' / 'cell' / 'candidate', case.solution)
                self.run = {'id': 'cell', 'status': 'ready' if not alive else 'planning', 'plan': [],
                            'verification': None, 'error': None,
                            'metrics': {'model_rounds': 1, 'reported_total_tokens': 8,
                                        'prompt_tokens': 4, 'completion_tokens': 4,
                                        'usage_complete': usage_complete}}
                return deepcopy(self.run)

            def get_run(self, identifier):
                return deepcopy(self.run)

            def cancel(self, identifier):
                return self.get_run(identifier)
        return FakeEngine, instances

    def test_batch_does_not_shrink_later_variant_budget(self):
        engine, instances = self.engine()
        config = BenchmarkConfig(variants=('baseline', 'full'), max_total_tokens=10, max_batch_tokens=15)
        with patch('benchmarks.runner.Engine', engine):
            report = run_suite(self.root / 'run', self.connection, [self.case], config)
        self.assertEqual(len(report['plan']), 2)
        self.assertEqual(len(report['results']), 1)
        self.assertEqual(instances[0].options['max_total_tokens'], 10)
        self.assertIn('equal-budget', report['stop_reason'])

    def test_missing_usage_stops_unbounded_followup_cells(self):
        engine, _ = self.engine(usage_complete=False)
        with patch('benchmarks.runner.Engine', engine):
            report = run_suite(self.root / 'run', self.connection, [self.case],
                               BenchmarkConfig(variants=('baseline', 'full')))
        self.assertEqual(len(report['results']), 1)
        self.assertIn('usage incomplete', report['stop_reason'])

    def test_still_inflight_observation_is_marked_partial(self):
        engine, _ = self.engine(alive=True)
        with patch('benchmarks.runner.Engine', engine):
            report = run_suite(self.root / 'run', self.connection, [self.case],
                               BenchmarkConfig(variants=('full',), task_timeout=1))
        self.assertFalse(report['results'][0]['success'])
        self.assertFalse(report['results'][0]['metrics']['usage_complete'])
        self.assertIn('in-flight', report['stop_reason'])

    def test_partial_selfcheck_is_not_cli_success(self):
        partial = {'results': [], 'plan': [{'task_id': 'pending'}]}
        with patch('benchmarks.__main__.self_check', return_value=partial):
            self.assertEqual(main(['--self-check', '--limit', '1', '--output', str(self.root / 'out')]), 1)

    def test_timed_out_seed_is_not_cli_selfcheck_success(self):
        plan = [{'task_id': 'fixture', 'category': 'bug_fix', 'variant': variant, 'repeat': 1}
                for variant in ('seed', 'reference')]
        report = {'schema_version': 1, 'mode': 'selfcheck', 'plan': plan, 'results': [
            {**plan[0], 'success': False, 'status': 'calibrated',
             'evaluator': {'passed': False, 'exit_code': -1, 'reason': 'timeout'}},
            {**plan[1], 'success': True, 'status': 'calibrated',
             'evaluator': {'passed': True, 'exit_code': 0, 'reason': None}},
        ]}
        with patch('benchmarks.__main__.self_check', return_value=report):
            self.assertEqual(main(['--self-check', '--limit', '1', '--output', str(self.root / 'out')]), 1)

    def test_selfcheck_requires_reference_public_tests(self):
        report = self_check(self.root / 'selfcheck', [self.case])
        reference = next(r for r in report['results'] if r['variant'] == 'reference')
        self.assertTrue(reference['evaluator']['passed'])
        self.assertFalse(reference['public_verification']['passed'])
        self.assertFalse(reference['success'])
        self.assertIn('No public tests discovered', reference['public_verification']['output'])

    def test_evaluator_timeout_terminates_owned_descendant_tree(self):
        case = TaskCase('tree', 'bug_fix', 'fixture', {'x.py': ''}, {},
                        'import subprocess, sys, time\n'
                        'from pathlib import Path\n'
                        'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\n'
                        'Path("descendant.pid").write_text(str(child.pid))\n'
                        'time.sleep(30)\n')
        write_files(self.root / 'candidate', case.files)
        result = evaluate(case, self.root / 'candidate', self.root / 'evaluation', timeout=1)
        self.assertFalse(result['passed'])
        self.assertEqual(result['reason'], 'timeout')
        self.assertTrue((self.root / 'evaluation' / 'candidate' / 'descendant.pid').exists())
        self.assertTrue(result['cleanup_confirmed'])


if __name__ == '__main__':
    unittest.main()
