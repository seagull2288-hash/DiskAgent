"""Windows snapshot sharing conflicts using temporary files and workflows."""
from copy import deepcopy
import ctypes
from ctypes import wintypes
import errno
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_app.schemas import TaskValidationError
from agent_app.state import WorkflowStore
from python_control import runtime


bridge = runtime.bridge


def windows_error(code):
    error = PermissionError(errno.EACCES, 'simulated Windows sharing conflict')
    error.winerror = code
    return error


class AtomicWriteRetryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='disk_atomic_retry_')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.store = WorkflowStore(SimpleNamespace(root=self.root))

    def test_transient_windows_conflicts_commit_one_revision_and_event(self):
        for code in (5, 32, 33):
            with self.subTest(winerror=code):
                state = self.store.create()
                path = self.store.directory(state.workflow_id) / 'state.json'
                original_bytes = path.read_bytes()
                original_replace = os.replace
                sources = []

                def transient_replace(source, destination):
                    sources.append(str(source))
                    self.assertEqual(path.read_bytes(), original_bytes)
                    self.assertEqual(state.revision, 0)
                    if len(sources) <= 2:
                        raise windows_error(code)
                    return original_replace(source, destination)

                with patch.object(bridge.sys, 'platform', 'win32'), patch.object(
                        bridge.os, 'replace', side_effect=transient_replace), patch.object(
                        bridge.time, 'sleep') as sleep:
                    self.store.record(state, 'planned', reason='one requested transition')
                self.assertEqual(len(sources), 3)
                self.assertEqual(len(set(sources)), 1)
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [.01, .02])
                loaded = self.store.load(state.workflow_id)
                self.assertEqual((loaded.revision, loaded.status), (1, 'planned'))
                self.assertEqual(len(loaded.events), 1)
                self.assertEqual(state.to_dict(), loaded.to_dict())
                self.assertEqual(list(path.parent.glob('state.json.*.tmp')), [])

    def test_exhausted_conflict_keeps_snapshot_caller_and_releases_lock(self):
        state = self.store.create()
        path = self.store.directory(state.workflow_id) / 'state.json'
        before = path.read_bytes()
        caller = deepcopy(state.to_dict())
        with patch.object(bridge.sys, 'platform', 'win32'), patch.object(
                bridge.os, 'replace', side_effect=windows_error(5)) as replace, patch.object(
                bridge.time, 'sleep') as sleep:
            with self.assertRaises(TaskValidationError) as caught:
                self.store.record(state, 'planned')
        self.assertEqual(caught.exception.code, 'WORKFLOW_IO')
        self.assertEqual(replace.call_count, 8)
        self.assertEqual(sleep.call_count, 7)
        self.assertAlmostEqual(sum(call.args[0] for call in sleep.call_args_list), .63)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(state.to_dict(), caller)
        self.assertEqual(list(path.parent.glob('state.json.*.tmp')), [])
        self.store.record(state, 'planned')
        self.assertEqual((state.revision, state.status), (1, 'planned'))

    def test_other_errors_and_non_windows_do_not_retry(self):
        source, destination = self.root / 'source', self.root / 'destination'
        for platform, error in [('linux', windows_error(5)),
                                ('win32', OSError(errno.ENOSPC, 'disk full')),
                                ('win32', PermissionError(errno.EACCES, 'no Windows error code'))]:
            with self.subTest(platform=platform, error=error):
                with patch.object(bridge.sys, 'platform', platform), patch.object(
                        bridge.os, 'replace', side_effect=error) as replace, patch.object(
                        bridge.time, 'sleep') as sleep:
                    with self.assertRaises(OSError):
                        bridge.atomic_replace(source, destination)
                self.assertEqual(replace.call_count, 1)
                sleep.assert_not_called()

    def test_json_checkpoint_writer_uses_the_same_bounded_retry(self):
        path = self.root / 'checkpoint.json'
        old = {'nCompleted': 4, 'bestStressMPa': 700.0}
        new = {'nCompleted': 5, 'bestStressMPa': 690.0}
        bridge.write_json(path, old)
        original_replace = os.replace
        attempts = []

        def transient_replace(source, destination):
            attempts.append(str(source))
            self.assertEqual(bridge.read_json(path), old)
            if len(attempts) == 1:
                raise windows_error(32)
            return original_replace(source, destination)

        with patch.object(bridge.sys, 'platform', 'win32'), patch.object(
                bridge.os, 'replace', side_effect=transient_replace), patch.object(bridge.time, 'sleep'):
            runtime.write_json(path, new)
        self.assertEqual(bridge.read_json(path), new)
        self.assertEqual(len(set(attempts)), 1)
        self.assertEqual(list(self.root.glob('checkpoint.json.*.tmp')), [])

    def test_exhausted_json_retry_preserves_last_checkpoint(self):
        path = self.root / 'checkpoint.json'
        old = {'nCompleted': 4}
        bridge.write_json(path, old)
        before = path.read_bytes()
        with patch.object(bridge.sys, 'platform', 'win32'), patch.object(
                bridge.os, 'replace', side_effect=windows_error(33)) as replace, patch.object(
                bridge.time, 'sleep'):
            with self.assertRaises(OSError):
                runtime.write_json(path, {'nCompleted': 5})
        self.assertEqual(replace.call_count, 8)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.root.glob('checkpoint.json.*.tmp')), [])

    @unittest.skipUnless(sys.platform == 'win32', 'Requires a real Windows sharing handle.')
    def test_real_windows_reader_can_close_between_replacement_attempts(self):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        state = self.store.create()
        path = self.store.directory(state.workflow_id) / 'state.json'
        # READ + WRITE sharing permits normal reads, while withholding DELETE
        # sharing reproduces the replacement conflict seen by the workbench.
        handle = kernel.CreateFileW(str(path), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        blocked, failures, calls = threading.Event(), [], []
        original_replace = os.replace

        def observe_replace(source, destination):
            calls.append(str(source))
            try:
                return original_replace(source, destination)
            except OSError as error:
                if getattr(error, 'winerror', None) in (5, 32, 33):
                    blocked.set()
                raise

        def writer():
            try:
                self.store.record(state, 'planned')
            except BaseException as error:
                failures.append(error)

        with patch.object(bridge.os, 'replace', side_effect=observe_replace):
            thread = threading.Thread(target=writer)
            thread.start()
            try:
                self.assertTrue(blocked.wait(5), 'The held reader must cause a real sharing conflict.')
                loaded = self.store.load(state.workflow_id)
                self.assertEqual((loaded.revision, loaded.status), (0, 'received'))
            finally:
                kernel.CloseHandle(handle)
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertGreaterEqual(len(calls), 2)
        loaded = self.store.load(state.workflow_id)
        self.assertEqual((loaded.revision, loaded.status), (1, 'planned'))
        self.assertEqual(len(loaded.events), 1)
        self.assertEqual(list(path.parent.glob('state.json.*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
