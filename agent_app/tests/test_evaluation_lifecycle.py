"""Evaluation reclamation and solver ownership survive interrupted local writes."""
from copy import deepcopy
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from python_control import evaluation_lifecycle as lifecycle
from python_control import runtime


class EvaluationAttemptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='disk_evaluation_lifecycle_')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.slot = self.root / 'eval_fixture'
        self.request = {'x': [1.0] * 9, 'project': 'fixture-only.wbpj',
                        'case_signature': 'fixture-case'}
        self.identity = patch.object(lifecycle, 'process_identity', return_value=None)
        self.process_identity = self.identity.start()
        self.addCleanup(self.identity.stop)

    def prepare(self, request=None):
        return lifecycle.prepare_attempt(self.root, self.slot, self.request if request is None else request)

    def owner(self, state='running', created=100):
        runtime.write_json(self.slot / 'process.json', {'state': state, 'pid': 42, 'created_ticks': created})

    def test_completed_attempt_is_reclaimed_only_for_the_exact_saved_request(self):
        attempt, reclaimed = self.prepare()
        self.assertEqual(attempt, self.slot)
        self.assertFalse(reclaimed)
        (attempt / 'complete.ok').write_text('WB2D_COMPLETE_V1', encoding='ascii')
        again, reclaimed = self.prepare()
        self.assertEqual(again, attempt)
        self.assertTrue(reclaimed)
        self.assertEqual(list(self.root.iterdir()), [self.slot])
        changed = deepcopy(self.request)
        changed['x'][0] = 2.0
        with self.assertRaisesRegex(runtime.ControllerError, 'frozen request'):
            self.prepare(changed)
        runtime.write_json(attempt / 'evaluation_request.json', changed)
        with self.assertRaisesRegex(runtime.ControllerError, 'pending candidate'):
            self.prepare()

    def test_live_exact_process_blocks_reclamation_even_with_a_completion_marker(self):
        self.prepare()
        self.owner()
        (self.slot / 'complete.ok').write_text('WB2D_COMPLETE_V1', encoding='ascii')
        self.process_identity.return_value = {'pid': 42, 'alive': True, 'created_ticks': 100}
        with self.assertRaisesRegex(runtime.ControllerError, 'still running'):
            self.prepare()
        self.assertEqual(list(self.root.iterdir()), [self.slot])

    def test_unknown_starting_process_keeps_the_existing_attempt_for_review(self):
        self.prepare()
        runtime.write_json(self.slot / 'process.json', {'state': 'starting'})
        with self.assertRaisesRegex(runtime.ControllerError, 'uncertain outcome'):
            self.prepare()
        self.assertEqual(list(self.root.iterdir()), [self.slot])

    def test_live_pid_without_saved_creation_time_keeps_ownership_uncertain(self):
        self.prepare()
        runtime.write_json(self.slot / 'process.json', {'state': 'running', 'pid': 42})
        self.process_identity.return_value = {'pid': 42, 'alive': True, 'created_ticks': 200}
        with self.assertRaisesRegex(runtime.ControllerError, 'identity is uncertain'):
            self.prepare()
        self.assertEqual(list(self.root.iterdir()), [self.slot])

    def test_confirmed_exit_creates_a_separate_retry_bound_to_the_same_slot(self):
        self.prepare()
        self.owner(state='exited')
        original = (self.slot / 'evaluation_request.json').read_bytes()
        attempt, reclaimed = self.prepare()
        self.assertFalse(reclaimed)
        self.assertNotEqual(attempt, self.slot)
        self.assertTrue(attempt.name.startswith(self.slot.name + '_retry_'))
        self.assertEqual((self.slot / 'evaluation_request.json').read_bytes(), original)
        self.assertEqual(runtime.read_json(attempt / 'evaluation_request.json'), self.request)
        manifest = runtime.read_json(self.slot / 'evaluation_slot.json')
        self.assertEqual(Path(manifest['current_attempt']), attempt)
        self.assertEqual(manifest['signature'], lifecycle.digest(self.request))

    def test_reused_pid_does_not_claim_that_the_old_solver_is_still_running(self):
        self.prepare()
        self.owner()
        self.process_identity.return_value = {'pid': 42, 'alive': True, 'created_ticks': 200}
        attempt, reclaimed = self.prepare()
        self.assertFalse(reclaimed)
        self.assertNotEqual(attempt, self.slot)
        runtime.write_json(attempt / 'monitor.json', {'status': 'running',
            'process': {'pid': 42, 'created_ticks': 100}})
        self.assertEqual(lifecycle.inspect_monitor(attempt)['status'], 'interrupted')


class EvaluationWaitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='disk_evaluation_wait_')
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.process = Mock(pid=42)
        self.process.wait.side_effect = [subprocess.TimeoutExpired('fixture solver', 2),
                                         subprocess.TimeoutExpired('fixture solver', 2), 0]
        self.ticks = 0

        def monotonic():
            self.ticks += 1
            return float(self.ticks)

        patches = [patch.object(lifecycle, 'process_identity', return_value={
                        'pid': 42, 'alive': True, 'created_ticks': 100}),
                   patch.object(lifecycle, '_stage', return_value='solve'),
                   patch.object(lifecycle, 'warning_limit', return_value=(0.5, 'fixture_threshold')),
                   patch.object(lifecycle.time, 'monotonic', side_effect=monotonic)]
        for block in patches:
            block.start()
            self.addCleanup(block.stop)

    def test_bounded_waits_publish_delayed_progress_and_return_the_solver_exit(self):
        published = []
        original_write = lifecycle.write_json

        def write(path, value):
            if Path(path).name == 'monitor.json':
                published.append(deepcopy(value))
            original_write(path, value)

        with patch.object(lifecycle, 'write_json', side_effect=write):
            code, stop, elapsed = lifecycle.wait_for_process(self.process, self.folder, 'fixture-case')
        self.assertEqual(code, 0)
        self.assertFalse(stop)
        self.assertGreater(elapsed, 0)
        self.assertEqual(self.process.wait.call_count, 3)
        self.assertTrue(all(call.kwargs == {'timeout': lifecycle.POLL_SECONDS}
                            for call in self.process.wait.call_args_list))
        self.assertIn('delayed', [item['status'] for item in published])
        self.assertEqual(published[-1]['status'], 'verifying')
        self.assertEqual(published[-1]['stage'], 'solve')
        self.assertEqual(published[-1]['case_signature'], 'fixture-case')
        owner = runtime.read_json(self.folder / 'process.json')
        self.assertEqual((owner['state'], owner['exit_code']), ('exited', 0))

    def test_monitor_write_failure_still_waits_until_the_owned_solver_exits(self):
        original_write = lifecycle.write_json

        def write(path, value):
            if Path(path).name == 'monitor.json':
                raise OSError('fixture monitor unavailable')
            original_write(path, value)

        with patch.object(lifecycle, 'write_json', side_effect=write):
            try:
                lifecycle.wait_for_process(self.process, self.folder, 'fixture-case')
            except OSError:
                pass
        self.assertEqual(self.process.wait.call_count, 3)
        self.process.terminate.assert_not_called()
        self.process.kill.assert_not_called()
        self.assertEqual(runtime.read_json(self.folder / 'process.json')['state'], 'exited')

    def test_initial_owner_write_failure_still_waits_until_the_owned_solver_exits(self):
        with patch.object(lifecycle, 'write_json', side_effect=OSError('fixture storage unavailable')):
            try:
                lifecycle.wait_for_process(self.process, self.folder, 'fixture-case')
            except OSError:
                pass
        self.assertEqual(self.process.wait.call_count, 3)
        self.process.terminate.assert_not_called()
        self.process.kill.assert_not_called()


if __name__ == '__main__':
    unittest.main()
