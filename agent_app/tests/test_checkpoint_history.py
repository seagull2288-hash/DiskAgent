"""Crash boundaries and verified checkpoint recovery use temporary fake solvers."""
from dataclasses import replace
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from agent_app.tests import test_engine_integration as fixture
from python_control import checkpoint_history, controller, runtime


class CheckpointHistoryTests(unittest.TestCase):
    setUp = fixture.ControllerIntegrationTests.setUp
    fake_proposal = fixture.ControllerIntegrationTests.fake_proposal
    fake_evaluate = fixture.ControllerIntegrationTests.fake_evaluate
    new_run = fixture.ControllerIntegrationTests.new_run

    def test_committed_backups_keep_five_verified_points_and_do_not_duplicate_requests(self):
        result = self.new_run(total=7)
        root = Path(result.data['run_dir'])
        backups = checkpoint_history.list_backups(root)
        self.assertEqual(backups['status'], 'passed')
        self.assertEqual([item['completed_candidates'] for item in backups['backups']], [7, 6, 5, 4, 3])
        for item in backups['backups']:
            path = root / 'checkpoint_backups' / item['backup_id']
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item['sha256'])
            self.assertIsNone(runtime.read_json(path)['pending'])
        state = runtime.read_json(root / 'checkpoint.json')
        before = sorted(path.name for path in (root / 'checkpoint_backups').iterdir())
        state['pending'] = {'phase': 'requesting'}
        controller.save_state(state)
        self.assertEqual(sorted(path.name for path in (root / 'checkpoint_backups').iterdir()), before)

    def test_tampered_backup_is_reported_and_cannot_replace_the_checkpoint(self):
        result = self.new_run(total=2)
        root = Path(result.data['run_dir'])
        original = (root / 'checkpoint.json').read_bytes()
        backup = checkpoint_history.list_backups(root)['backups'][0]
        path = root / 'checkpoint_backups' / backup['backup_id']
        path.write_bytes(path.read_bytes() + b' ')
        self.assertEqual(checkpoint_history.list_backups(root)['status'], 'warning')
        with self.assertRaisesRegex(runtime.ControllerError, 'digest'):
            checkpoint_history.restore_backup(self.settings, root, backup['backup_id'])
        self.assertEqual((root / 'checkpoint.json').read_bytes(), original)

    def test_explicit_restore_replays_native_checks_and_preserves_previous_checkpoint(self):
        result = self.new_run(total=2)
        root = Path(result.data['run_dir'])
        backup_id = 'checkpoint_000002.json'
        original = (root / 'checkpoint.json').read_bytes()
        before = self.evaluations, self.proposals
        preview = checkpoint_history.preview_restore(self.settings, root, backup_id)
        self.assertEqual(preview['completed_candidates'], 2)
        self.assertEqual(preview['target_total_candidates'], 2)
        self.assertTrue(preview['can_restore'])
        self.assertEqual((root / 'checkpoint.json').read_bytes(), original)
        restored = checkpoint_history.restore_backup(self.settings, root, backup_id,
            expected_current_sha256=preview['current_checkpoint_sha256'])
        self.assertTrue(restored['restored'])
        self.assertEqual(Path(restored['previous_checkpoint_file']).read_bytes(), original)
        state = runtime.read_json(root / 'checkpoint.json')
        self.assertEqual(state['nCompleted'], 2)
        self.assertEqual(state['status'], 'interrupted')
        self.assertEqual((self.evaluations, self.proposals), before)
        with patch.object(controller, 'verify_native', side_effect=runtime.ControllerError('Fixture bad native evidence')):
            with self.assertRaisesRegex(runtime.ControllerError, 'bad native'):
                checkpoint_history.preview_restore(self.settings, root, backup_id)

    def test_restore_rejects_running_state_and_a_changed_engine_fingerprint(self):
        result = self.new_run(total=1)
        root = Path(result.data['run_dir'])
        state = runtime.read_json(root / 'checkpoint.json')
        state['status'] = 'running'
        runtime.write_json(root / 'checkpoint.json', state)
        with self.assertRaisesRegex(runtime.ControllerError, '计算中'):
            checkpoint_history.restore_backup(self.settings, root, 'checkpoint_000001.json')
        with patch.object(controller, 'fingerprint', return_value={'changed': 'engine'}):
            with self.assertRaisesRegex(runtime.ControllerError, 'changed'):
                checkpoint_history.preview_restore(self.settings, root, 'checkpoint_000001.json')

    def test_restore_requires_a_missing_checkpoint_to_still_be_missing_after_preview(self):
        result = self.new_run(total=1)
        root = Path(result.data['run_dir'])
        checkpoint = root / 'checkpoint.json'
        original = checkpoint.read_bytes()
        checkpoint.unlink()
        preview = checkpoint_history.preview_restore(self.settings, root, 'checkpoint_000001.json')
        self.assertIsNone(preview['current_checkpoint_sha256'])
        checkpoint.write_bytes(original)
        with self.assertRaisesRegex(runtime.ControllerError, 'changed after'):
            checkpoint_history.restore_backup(self.settings, root, 'checkpoint_000001.json',
                expected_current_sha256=preview['current_checkpoint_sha256'])
        self.assertEqual(checkpoint.read_bytes(), original)
        checkpoint.unlink()
        restored = checkpoint_history.restore_backup(self.settings, root, 'checkpoint_000001.json',
            expected_current_sha256=preview['current_checkpoint_sha256'])
        self.assertTrue(restored['restored'])
        self.assertEqual(runtime.read_json(checkpoint)['nCompleted'], 1)

    def test_older_backups_remain_readable_but_cannot_roll_back_committed_candidates(self):
        result = self.new_run(total=2)
        root = Path(result.data['run_dir'])
        original = (root / 'checkpoint.json').read_bytes()
        preview = checkpoint_history.preview_restore(self.settings, root, 'checkpoint_000001.json')
        self.assertEqual(preview['verification'], 'passed')
        self.assertFalse(preview['can_restore'])
        self.assertIn('最新', preview['reason_zh'])
        with self.assertRaisesRegex(runtime.ControllerError, '最新'):
            checkpoint_history.restore_backup(self.settings, root, 'checkpoint_000001.json')
        self.assertEqual((root / 'checkpoint.json').read_bytes(), original)

    def test_next_iteration_evidence_blocks_backup_restore_and_blind_resume(self):
        result = self.new_run(total=1)
        root = Path(result.data['run_dir'])
        directory = root / 'iterations' / 'iter_002'
        directory.mkdir()
        evidence = directory / 'candidate.json'
        evidence.write_text('fixture uncommitted evidence', encoding='utf-8')
        original = (root / 'checkpoint.json').read_bytes()
        preview = checkpoint_history.preview_restore(self.settings, root, 'checkpoint_000001.json')
        self.assertEqual(preview['verification'], 'passed')
        self.assertFalse(preview['can_restore'])
        self.assertIn('未结算现场', preview['reason_zh'])
        with self.assertRaisesRegex(runtime.ControllerError, '未结算现场'):
            checkpoint_history.restore_backup(self.settings, root, 'checkpoint_000001.json')
        self.assertEqual((root / 'checkpoint.json').read_bytes(), original)
        before = self.evaluations, self.proposals
        with self.assertRaisesRegex(runtime.ControllerError, '未结算现场'):
            controller.run(replace(self.settings, candidate_count=2), resume=root,
                           proposal_fn=self.fake_proposal, evaluator=self.fake_evaluate)
        self.assertEqual((self.evaluations, self.proposals), before)
        self.assertEqual(evidence.read_text(encoding='utf-8'), 'fixture uncommitted evidence')

    def test_backup_write_failure_does_not_undo_a_committed_solver_result(self):
        with patch.object(checkpoint_history, 'write_json', side_effect=OSError('Fixture backup disk failure')):
            result = self.new_run(total=1)
        state = runtime.read_json(Path(result.data['run_dir']) / 'checkpoint.json')
        self.assertEqual(state['status'], 'complete')
        self.assertEqual(state['nCompleted'], 1)
        self.assertEqual(len(state['records']), 1)

    def test_default_solver_receives_the_same_durable_candidate_directory_after_interruption(self):
        settings = replace(self.settings, candidate_count=1)
        directories = []
        interrupted = False

        def fake_native(x, settings, problem, output_root, baseline=None, *, evaluation_dir=None):
            nonlocal interrupted
            self.assertIsNotNone(evaluation_dir)
            snapshot = runtime.read_json(next(self.output.glob('run_*')) / 'checkpoint.json')
            saved = snapshot['baseline_evaluation'] if baseline is None else snapshot['pending']
            self.assertEqual(saved['evaluation_dir'], evaluation_dir)
            self.assertEqual(Path(evaluation_dir).name, saved['evaluation_id'])
            if baseline is not None:
                directories.append(evaluation_dir)
                if not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt('Fixture crash after evaluation directory reservation.')
            return self.fake_evaluate(x, settings, problem, output_root, baseline)

        with patch.object(controller, 'evaluate', side_effect=fake_native):
            with self.assertRaises(KeyboardInterrupt):
                controller.run(settings, proposal_fn=self.fake_proposal)
            root = next(self.output.glob('run_*'))
            proposals = self.proposals
            controller.run(settings, resume=root, proposal_fn=self.fake_proposal)
        self.assertEqual(directories[0], directories[1])
        self.assertEqual(self.proposals, proposals)
        self.assertEqual(runtime.read_json(root / 'checkpoint.json')['nCompleted'], 1)


if __name__ == '__main__':
    unittest.main()
