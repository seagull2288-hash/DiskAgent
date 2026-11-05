"""Durability, containment and process-lock tests using temporary workflows only."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import fields
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_app import state as module
from agent_app.schemas import TaskSpec, TaskValidationError
from agent_app.state import WorkflowState, WorkflowStore


PROJECT = Path(__file__).resolve().parents[2]
STAMP = '2026-11-04T20:00:00+08:00'


class StateValidationTests(unittest.TestCase):
    def state(self, **changes):
        value = {'workflow_id': 'wf_test', 'created_at': STAMP, 'updated_at': STAMP}
        value.update(changes)
        return WorkflowState.from_dict(value)

    def test_defaults_task_normalization_and_detached_round_trip(self):
        task = TaskSpec('optimize', total_candidate_budget=8)
        value = self.state(task=task, planning={'context': [1]}, result={'ok': True})
        self.assertEqual(value.revision, 0)
        self.assertEqual(value.status, 'received')
        self.assertEqual(value.batch_size, 4)
        self.assertEqual(value.task, task.to_dict())
        saved = value.to_dict()
        self.assertEqual(WorkflowState.from_dict(saved), value)
        saved['planning']['context'].append(2)
        self.assertEqual(value.planning['context'], [1])

    def test_dataclass_construction_cannot_bypass_validation(self):
        value = WorkflowState('wf_test', STAMP, STAMP, batch_size=True)
        with self.assertRaises(TaskValidationError) as caught:
            value.to_dict()
        self.assertEqual(caught.exception.code, 'WORKFLOW_COUNTS')

    def test_legacy_state_defaults_do_not_enable_or_invent_feedback(self):
        legacy = self.state().to_dict()
        legacy.pop('feedback_policy')
        legacy.pop('decision_trace')
        restored = WorkflowState.from_dict(legacy)
        self.assertIsNone(restored.feedback_policy)
        self.assertEqual(restored.decision_trace, [])
        other = self.state()
        restored.decision_trace.append({'source_kind': 'cached_legacy'})
        self.assertEqual(other.decision_trace, [])
        self.assertIsNone(restored.to_dict()['feedback_policy'])

    def test_feedback_and_trace_keep_finite_json_evidence_and_copy_isolation(self):
        policy = {'schema_version': 'disk-agent.feedback-policy.v1', 'enabled': True,
                  'feedback_schema': 'disk-agent.batch-feedback.v1', 'max_recent': 3}
        trace = [{'decision_id': 'decision_2', 'generation': 2, 'run_id': 'run_test',
                  'feedback_id': 'feedback_4', 'feedback_summary': '上批4个候选已提交。',
                  'feedback_source_completed_candidates': 4, 'source_kind': 'feedback_augmented',
                  'center': {'X1': 4.25}, 'rationale': '减小X1可能降低应力。',
                  'sigma_fraction': 0.08, 'feedback_file': None, 'request_status': 'new_request',
                  'outcome': {'status': 'pending', 'completed_candidates': 0,
                              'candidate_iterations': [], 'best_stress_mpa': None}}]
        value = self.state(feedback_policy=policy, decision_trace=trace)
        policy['enabled'] = False
        trace[0]['outcome']['candidate_iterations'].append(5)
        self.assertTrue(value.feedback_policy['enabled'])
        self.assertEqual(value.decision_trace[0]['outcome']['candidate_iterations'], [])
        self.assertEqual(WorkflowState.from_dict(value.to_dict()), value)
        for change in ({'feedback_policy': []}, {'feedback_policy': True},
                       {'decision_trace': {}}, {'decision_trace': [None]},
                       {'feedback_policy': {'weight': float('inf')}},
                       {'decision_trace': [{'center': {'X1': float('nan')}}]},
                       {'decision_trace': [{'outcome': {'stress': -float('inf')}}]}):
            with self.subTest(change=str(change)), self.assertRaises(TaskValidationError):
                self.state(**change)

    def test_rejects_unknown_fields_schema_required_fields_and_live_grant(self):
        valid = self.state().to_dict()
        for change in ({'live': True}, {'allow_execution': True}, {'schema_version': 'future'},
                       {'status': 'paused'}, {'status': []}):
            with self.subTest(change=change), self.assertRaises(TaskValidationError):
                WorkflowState.from_dict(dict(valid, **change))
        for missing in ('workflow_id', 'created_at', 'updated_at'):
            raw = dict(valid)
            del raw[missing]
            with self.subTest(missing=missing), self.assertRaises(TaskValidationError):
                WorkflowState.from_dict(raw)
        self.assertNotIn('live', {item.name for item in fields(WorkflowState)})

    def test_count_types_exclude_boolean_float_and_invalid_ranges(self):
        for name, bad in (('revision', [True, -1, 1.0, '1']),
                          ('start_completed_candidates', [False, -1, 1.0, None]),
                          ('batch_size', [True, 0, -1, 1.0]),
                          ('target_total_candidates', [True, 0, -1, 2.0])):
            for value in bad:
                with self.subTest(name=name, value=value), self.assertRaises(TaskValidationError):
                    self.state(**{name: value})
        with self.assertRaises(TaskValidationError):
            self.state(start_completed_candidates=3, target_total_candidates=2)
        self.state(start_completed_candidates=3, target_total_candidates=3)

    def test_path_names_and_task_validation_are_not_weakened(self):
        for name in ('../wf_test', 'wf_test/child', 'wf_test\\child', 'C:\\wf_test',
                     'wf_test.', 'wf_', '', 4):
            with self.subTest(name=name), self.assertRaises(TaskValidationError):
                self.state(workflow_id=name)
        for run_id in ('../run_test', 'run_test/child', 'run_test.', True):
            with self.subTest(run_id=run_id), self.assertRaises(TaskValidationError):
                self.state(run_id=run_id)
        for task in ({'operation': 'optimize', 'total_candidate_budget': True},
                     {'operation': 'check', 'case_id': 'other'},
                     {'operation': 'check', 'live': True}):
            with self.subTest(task=task), self.assertRaises(TaskValidationError):
                self.state(task=task)
        with self.assertRaises(TaskValidationError):
            self.state(task={'operation': 'status', 'run_id': 'run_a'}, run_id='run_b')

    def test_json_values_reject_nonfinite_cycles_coerced_keys_and_objects(self):
        cycle = []
        cycle.append(cycle)
        for value in ({'x': float('nan')}, {'x': [float('inf')]}, {'x': -float('inf')},
                      {1: 'coerced key'}, {'x': Path('result')}, {'x': cycle},
                      {'x': object()}, {'x': (1, 2)}):
            with self.subTest(value=str(type(value))), self.assertRaises(TaskValidationError):
                self.state(analysis=value)
        deeply_nested = value = {}
        for _ in range(70):
            value['child'] = {}
            value = value['child']
        with self.assertRaises(TaskValidationError):
            self.state(analysis=deeply_nested)

    def test_container_types_requests_timestamps_and_flags(self):
        invalid = [{'text_plan_only': 1}, {'warnings': [42]}, {'warnings': 'warning'},
                   {'events': [42]}, {'batches': {}}, {'planning': []}, {'pending_batch': []},
                   {'analysis': []}, {'result': []}, {'error': 'error'},
                   {'request_text': ''}, {'request_text': 'x\0y'}, {'request_text': 'a' * 4001},
                   {'created_at': 'yesterday'}, {'updated_at': '2026-11-04T12:00:00'},
                   {'updated_at': '2026-11-03T20:00:00+08:00'},
                   {'events': [{} for _ in range(501)]}]
        for change in invalid:
            with self.subTest(change=str(change)[:100]), self.assertRaises(TaskValidationError):
                self.state(**change)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='disk_workflow_test_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = SimpleNamespace(root=self.root)
        self.store = WorkflowStore(self.settings)

    def test_create_is_unique_initial_revision_zero_and_round_trips(self):
        first = self.store.create('优化八个候选')
        second = self.store.create()
        self.assertNotEqual(first.workflow_id, second.workflow_id)
        self.assertEqual(first.revision, 0)
        self.assertEqual(self.store.load(first.workflow_id), first)
        self.assertEqual(self.store.directory(first.workflow_id).parent,
                         self.root / 'agent_workflows')
        self.assertTrue((self.store.directory(first.workflow_id) / 'state.json').is_file())
        self.assertFalse((self.root / 'llm_optimization.lock').exists())

    def test_invalid_create_has_no_filesystem_side_effect(self):
        with self.assertRaises(TaskValidationError):
            self.store.create('')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_feedback_policy_and_measured_trace_persist_without_upgrading_legacy(self):
        value = self.store.create()
        path = self.store.directory(value.workflow_id) / 'state.json'
        legacy = value.to_dict()
        legacy.pop('feedback_policy')
        legacy.pop('decision_trace')
        path.write_text(json.dumps(legacy), encoding='utf-8')
        restored = self.store.load(value.workflow_id)
        self.store.save(restored)
        self.assertIsNone(self.store.load(value.workflow_id).feedback_policy)
        self.assertEqual(self.store.load(value.workflow_id).decision_trace, [])
        restored.feedback_policy = {'schema_version': 'disk-agent.feedback-policy.v1', 'enabled': True,
                                     'feedback_schema': 'disk-agent.batch-feedback.v1', 'max_recent': 3}
        restored.decision_trace = [{'generation': 1, 'feedback_id': None, 'source_kind': 'initial_lhs',
                                    'outcome': {'status': 'pending', 'completed_candidates': 0}}]
        self.store.save(restored)
        snapshot = self.store.load(value.workflow_id)
        snapshot.decision_trace[0]['outcome'] = {'status': 'completed', 'completed_candidates': 4,
                                                'candidate_iterations': [1, 2, 3, 4], 'best_stress_mpa': 680.5}
        self.store.save(snapshot)
        self.assertEqual(self.store.load(value.workflow_id).decision_trace[0]['outcome']['best_stress_mpa'], 680.5)
        with self.assertRaises(TaskValidationError) as caught:
            self.store.save(restored)
        self.assertEqual(caught.exception.code, 'WORKFLOW_STALE')

    def test_save_updates_same_object_and_rejects_stale_snapshot(self):
        current = self.store.create()
        stale = self.store.load(current.workflow_id)
        current.status = 'planned'
        self.assertIs(self.store.save(current), current)
        self.assertEqual(current.revision, 1)
        self.assertGreaterEqual(current.updated_at, current.created_at)
        stale.status = 'failed'
        with self.assertRaises(TaskValidationError) as caught:
            WorkflowStore(self.settings).save(stale)
        self.assertEqual(caught.exception.code, 'WORKFLOW_STALE')
        self.assertEqual(stale.revision, 0)
        self.assertEqual(self.store.load(current.workflow_id).status, 'planned')
        current.created_at = '2026-01-01T00:00:00+00:00'
        with self.assertRaises(TaskValidationError) as caught:
            self.store.save(current)
        self.assertEqual(caught.exception.code, 'WORKFLOW_TIME')

    def test_atomic_replace_failure_preserves_committed_bytes_and_revision(self):
        value = self.store.create()
        path = self.store.directory(value.workflow_id) / 'state.json'
        before = path.read_bytes()
        value.status = 'running'
        with patch.object(module.runtime.os, 'replace', side_effect=OSError('simulated replace failure')):
            with self.assertRaises(TaskValidationError) as caught:
                self.store.save(value)
        self.assertEqual(caught.exception.code, 'WORKFLOW_IO')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(value.revision, 0)
        self.assertEqual(list(path.parent.glob('state.json.*.tmp')), [])
        self.store.save(value)
        self.assertEqual(self.store.load(value.workflow_id).status, 'running')

    def test_load_reads_previous_atomic_snapshot_without_waiting_for_writer(self):
        value = self.store.create()
        value.status = 'planned'
        replacing, release = threading.Event(), threading.Event()
        original_replace = module.runtime.os.replace
        errors = []

        def hold_replace(source, destination):
            replacing.set()
            if not release.wait(5):
                raise RuntimeError('test writer was not released')
            return original_replace(source, destination)

        def write():
            try:
                self.store.save(value)
            except BaseException as exc:
                errors.append(exc)

        with patch.object(module.runtime.os, 'replace', side_effect=hold_replace):
            writer = threading.Thread(target=write)
            writer.start()
            try:
                self.assertTrue(replacing.wait(5))
                loaded = WorkflowStore(self.settings).load(value.workflow_id)
                self.assertEqual((loaded.revision, loaded.status), (0, 'received'))
            finally:
                release.set()
                writer.join(5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        loaded = self.store.load(value.workflow_id)
        self.assertEqual((loaded.revision, loaded.status), (1, 'planned'))

    def test_record_preserves_details_trims_history_and_returns_same_state(self):
        value = self.store.create()
        value.events = [{'status': 'planning', 'details': {'index': i}} for i in range(500)]
        self.store.save(value)
        info = {'items': ['a']}
        self.assertIs(self.store.record(value, 'validated', reason=info), value)
        self.assertEqual(value.revision, 2)
        self.assertEqual(value.status, 'validated')
        self.assertEqual(len(value.events), 500)
        self.assertEqual(value.events[0]['details']['index'], 1)
        self.assertEqual(value.events[-1]['details']['reason'], info)
        info['items'].append('b')
        self.assertEqual(value.events[-1]['details']['reason']['items'], ['a'])
        self.assertEqual(self.store.load(value.workflow_id), value)

    def test_failed_record_does_not_publish_new_status_event_or_revision(self):
        value = self.store.create()
        before = deepcopy(value.to_dict())
        for status, details in (('unsupported', {}), ('planned', {'bad': float('nan')})):
            with self.subTest(status=status), self.assertRaises(TaskValidationError):
                self.store.record(value, status, **details)
            self.assertEqual(value.to_dict(), before)
        self.store.record(self.store.load(value.workflow_id), 'planned')
        with self.assertRaises(TaskValidationError) as caught:
            self.store.record(value, 'failed', reason='stale')
        self.assertEqual(caught.exception.code, 'WORKFLOW_STALE')
        self.assertEqual(value.to_dict(), before)

    def test_load_rejects_corrupt_duplicate_nonfinite_or_mismatched_state(self):
        value = self.store.create()
        path = self.store.directory(value.workflow_id) / 'state.json'
        valid = value.to_dict()
        invalid = ['{', '{"workflow_id":"wf_a","workflow_id":"wf_b"}',
                   json.dumps(dict(valid, analysis={'value': float('nan')})),
                   json.dumps(dict(valid, workflow_id='wf_another')),
                   json.dumps(dict(valid, live=True))]
        for text in invalid:
            path.write_text(text, encoding='utf-8')
            with self.subTest(text=text[:50]), self.assertRaises(TaskValidationError):
                self.store.load(value.workflow_id)

    def test_missing_and_path_inputs_return_validation_errors(self):
        for workflow_id in ('../wf_escape', 'wf_escape/child', 'C:\\wf_escape', 'wf_escape.'):
            with self.subTest(workflow_id=workflow_id), self.assertRaises(TaskValidationError):
                self.store.directory(workflow_id)
        with self.assertRaises(TaskValidationError) as caught:
            self.store.load('wf_missing')
        self.assertEqual(caught.exception.code, 'WORKFLOW_NOT_FOUND')
        with self.assertRaises(TaskValidationError):
            with self.store.lock('wf_missing'):
                self.fail('missing workflow must not be locked')

    def directory_link(self, link, target):
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError as exc:
            if sys.platform != 'win32':
                self.skipTest('Directory symlinks unavailable: ' + str(exc))
            result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(target)],
                                    capture_output=True, timeout=10)
            if result.returncode:
                self.skipTest('Directory symlink/junction creation is unavailable.')
        # All created links are inside this temporary test tree. Remove only
        # the link itself, never recurse into its target.
        self.addCleanup(lambda: os.rmdir(link) if link.exists() else None)
        self.assertEqual(link.resolve(), target.resolve())

    def test_directory_junction_escape_is_rejected(self):
        (self.root / 'agent_workflows').mkdir()
        outside = self.root / 'other_location'
        outside.mkdir()
        self.directory_link(self.root / 'agent_workflows' / 'wf_escape', outside)
        with self.assertRaises(TaskValidationError) as caught:
            self.store.directory('wf_escape')
        self.assertEqual(caught.exception.code, 'WORKFLOW_PATH')

    def test_storage_root_junction_escape_is_rejected(self):
        outside = self.root / 'other_location'
        outside.mkdir()
        self.directory_link(self.root / 'agent_workflows', outside)
        with self.assertRaises(TaskValidationError) as caught:
            self.store.create()
        self.assertEqual(caught.exception.code, 'WORKFLOW_PATH')
        self.assertEqual(list(outside.iterdir()), [])

    def test_workflow_lock_is_nonblocking_scoped_and_released_on_exception(self):
        first, second = self.store.create(), self.store.create()
        with self.assertRaisesRegex(RuntimeError, 'body failed'):
            with self.store.lock(first.workflow_id):
                with self.assertRaises(TaskValidationError) as caught:
                    with self.store.lock(first.workflow_id):
                        self.fail('second lock must not enter')
                self.assertEqual(caught.exception.code, 'WORKFLOW_BUSY')
                with self.store.lock(second.workflow_id):
                    self.store.record(first, 'planned')
                    self.assertEqual(self.store.load(first.workflow_id).status, 'planned')
                raise RuntimeError('body failed')
        with self.store.lock(first.workflow_id):
            self.assertEqual(self.store.load(first.workflow_id).revision, 1)
        self.assertFalse((self.root / 'llm_optimization.lock').exists())

    def child_lock(self, workflow_id):
        script = '''
import sys
from pathlib import Path
from types import SimpleNamespace
from agent_app.state import WorkflowStore
from agent_app.schemas import TaskValidationError
store = WorkflowStore(SimpleNamespace(root=Path(sys.argv[1])))
try:
    with store.lock(sys.argv[2]):
        print('acquired')
except TaskValidationError as exc:
    print(exc.code)
'''
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.root), workflow_id],
                                cwd=PROJECT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_os_lock_blocks_separate_process_and_recovers_after_release(self):
        value = self.store.create()
        with self.store.lock(value.workflow_id):
            self.assertEqual(self.child_lock(value.workflow_id), 'WORKFLOW_BUSY')
        self.assertEqual(self.child_lock(value.workflow_id), 'acquired')

    def test_os_lock_is_released_when_owner_process_exits_without_cleanup(self):
        value = self.store.create()
        script = '''
import os, sys
from pathlib import Path
from types import SimpleNamespace
from agent_app.state import WorkflowStore
store = WorkflowStore(SimpleNamespace(root=Path(sys.argv[1])))
with store.lock(sys.argv[2]):
    print('acquired', flush=True)
    os._exit(0)
'''
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.root), value.workflow_id],
                                cwd=PROJECT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'acquired')
        with self.store.lock(value.workflow_id):
            self.assertEqual(self.store.load(value.workflow_id).revision, 0)


if __name__ == '__main__':
    unittest.main()
