"""Durable evaluation ownership, progress observation and result reclamation."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .runtime import ControllerError, read_json, write_json


POLL_SECONDS = 2
DEFAULT_WARNING_SECONDS = 1800
STAGE_LABELS = {
    'launching': '启动 Workbench', 'open_project': '打开工程',
    'apply_parameters': '写入参数', 'geometry_update': '更新几何',
    'model_refresh': '刷新模型', 'workbench_update': '更新设计点并求解',
    'native_verification': '读取原生结果', 'preflight': '核验模型',
    'preflight_verified': '模型核验完成', 'generate_missing_mesh': '生成网格',
    'clear_generated_solution_data': '准备求解', 'solve': '结构求解',
    'reuse_fresh_workbench_update': '核验本次设计点结果',
    'evaluate_existing_results': '读取结果', 'verify_fresh_solution': '核验结果新鲜度',
    'collect_native_axisymmetric_mass': '读取整盘质量',
    'solve_configured_modal_analysis': '模态求解', 'export_results': '导出结果',
    'collect_mesh_quality': '读取网格质量',
    'complete': '结果核验完成',
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def evaluation_signature(settings, problem, mesh_size_mm):
    root, project = Path(settings.root), Path(settings.project_file)
    sources = [project, root / 'make_verified_journal.py', root / 'mechanical_template.py.txt',
               root / 'api_bridge' / 'llm_bridge.py']
    sources += list((root / 'python_control').glob('*.py')) + list((root / 'cad').glob('*.prt'))
    material_root = project.parent / (project.stem + '_files')
    sources += list(material_root.rglob('*.engd')) + list(material_root.rglob('EngineeringData.xml'))
    hashes = {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(set(sources)) if path.is_file()}
    contract = {key: problem[key] for key in ('variables', 'input_order', 'workbench_parameter_ids',
                'decimals', 'optimization_policy') if key in problem}
    return digest({'project': str(project.resolve()), 'source_hashes': hashes, 'problem': contract,
                   'policy': getattr(settings, 'optimization_policy', None), 'mesh_size_mm': mesh_size_mm})


def process_identity(pid):
    """Identify the exact Windows process, including creation time to detect PID reuse."""
    if sys.platform != 'win32' or type(pid) is not int or pid <= 0:
        return None
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:
            return {'pid': pid, 'alive': False}
        return None
    try:
        code = wintypes.DWORD()
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(system), ctypes.byref(user)):
            return None
        return {'pid': pid, 'alive': code.value == 259,
                'created_ticks': (created.dwHighDateTime << 32) | created.dwLowDateTime}
    finally:
        kernel.CloseHandle(handle)


def read_optional(path):
    from .runtime import bridge
    try:
        value = read_json(path)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, bridge.BridgeError):
        return {}


def prepare_attempt(output_root, evaluation_dir, request):
    """Reuse a bound completed attempt, or prepare a fresh, separately audited attempt."""
    output_root = Path(output_root).resolve()
    if evaluation_dir is not None and Path(evaluation_dir).is_symlink():
        raise ControllerError('Evaluation folder cannot be a symbolic link.')
    slot = (Path(evaluation_dir).resolve() if evaluation_dir is not None else
            output_root / ('eval_' + uuid.uuid4().hex))
    if slot.parent != output_root or not slot.name.startswith('eval_') or slot.is_symlink():
        raise ControllerError('Evaluation identity must select a direct evaluation folder.')
    signature = digest(request)
    manifest_path = slot / 'evaluation_slot.json'
    if slot.exists():
        if manifest_path.is_symlink():
            raise ControllerError('Evaluation manifest cannot redirect outside its folder.')
        manifest = read_optional(manifest_path)
        if manifest.get('signature') != signature:
            raise ControllerError('Saved evaluation differs from its frozen request; inspect before continuing.')
        saved_attempt = Path(manifest.get('current_attempt', ''))
        if saved_attempt.is_symlink():
            raise ControllerError('Evaluation attempt cannot be a symbolic link.')
        attempt = saved_attempt.resolve()
        if (attempt.parent != output_root or not attempt.name.startswith(slot.name)
                or not attempt.is_dir() or attempt.is_symlink()):
            raise ControllerError('Saved evaluation attempt path is invalid.')
        if read_optional(attempt / 'evaluation_request.json') != request:
            raise ControllerError('Saved evaluation request differs from the pending candidate.')
        owner = read_optional(attempt / 'process.json')
        identity = process_identity(owner.get('pid'))
        saved_ticks = owner.get('created_ticks')
        actual_ticks = (identity or {}).get('created_ticks')
        identified = type(saved_ticks) is int and type(actual_ticks) is int
        if identity and identity.get('alive'):
            if identified and actual_ticks == saved_ticks:
                raise ControllerError('This exact Workbench process is still running; wait before continuing.')
            if not identified and owner.get('state') != 'exited':
                raise ControllerError('Workbench process identity is uncertain; inspect before continuing.')
        if (attempt / 'complete.ok').is_file():
            return attempt, True
        known_stopped = (owner.get('state') in ('exited', 'not_started') or
                         identity is not None and (not identity.get('alive') or
                         identified and actual_ticks != saved_ticks))
        if not known_stopped:
            raise ControllerError('Previous Workbench launch has an uncertain outcome; inspect its process and logs.')
        attempt = output_root / (slot.name + '_retry_' + uuid.uuid4().hex[:12])
        attempt.mkdir(parents=True, exist_ok=False)
    else:
        slot.mkdir(parents=True, exist_ok=False)
        attempt = slot
    write_json(attempt / 'evaluation_request.json', request)
    write_json(attempt / 'process.json', {'state': 'not_started'})
    write_json(manifest_path, {'schema_version': 'disk.evaluation-slot.v1', 'signature': signature,
                              'current_attempt': str(attempt), 'updated_utc': utc_now()})
    return attempt, False


def _stage(folder):
    sources = []
    for name in ('workbench_progress.json', 'mechanical_verified.json'):
        path = folder / name
        value = read_optional(path)
        if isinstance(value.get('stage'), str):
            try:
                sources.append((path.stat().st_mtime_ns, value['stage']))
            except OSError:
                pass
    return max(sources)[1] if sources else 'launching'


def warning_limit(folder, stage, case_signature):
    values = []
    try:
        for path in sorted(folder.parent.glob('eval_*/monitor.json'), key=lambda item: item.stat().st_mtime_ns,
                           reverse=True)[:12]:
            previous = read_optional(path)
            if previous.get('status') != 'completed' or previous.get('case_signature') != case_signature:
                continue
            for item in previous.get('stages', []):
                if (isinstance(item, dict) and item.get('stage') == stage
                        and type(item.get('seconds')) in (int, float) and 0 <= item['seconds'] < 86400):
                    values.append(item['seconds'])
    except (OSError, ValueError, TypeError):
        return DEFAULT_WARNING_SECONDS, 'default_observation_threshold'
    if len(values) < 3:
        return DEFAULT_WARNING_SECONDS, 'default_observation_threshold'
    return max(180, max(values) * 3 + 60), 'recent_same_case_stage_durations'


def wait_for_process(process, folder, case_signature):
    """Observe progress with bounded waits; prolonged calculation triggers a diagnostic."""
    folder = Path(folder)
    identity = process_identity(process.pid) or {'pid': process.pid}
    owner = dict(identity, state='running', started_utc=utc_now())
    monitor_error = _write_diagnostic(folder / 'process.json', owner)
    start = phase_start = last_progress = time.monotonic()
    stage = _stage(folder)
    stage_started = last_progress_utc = utc_now()
    stages = []
    signatures = None
    stop_requested = False
    limit, limit_source = warning_limit(folder, stage, case_signature)

    def snapshot(status):
        now = time.monotonic()
        return {'schema_version': 'disk.evaluation-monitor.v1', 'status': status,
                'stage': stage, 'stage_label': STAGE_LABELS.get(stage, stage),
                'started_utc': owner['started_utc'], 'updated_utc': utc_now(),
                'stage_started_utc': stage_started, 'last_progress_utc': last_progress_utc,
                'elapsed_seconds': now - start, 'stage_elapsed_seconds': now - phase_start,
                'seconds_since_progress': now - last_progress, 'warning_limit_seconds': limit,
                'threshold_source': limit_source, 'case_signature': case_signature,
                'process': identity, 'stages': stages,
                'message': ('当前阶段超过观察阈值，正在核对进程和日志。' if status == 'delayed' else ''),
                'monitor_write_error': monitor_error, 'evaluation_directory': str(folder)}

    initial_error = _write_diagnostic(folder / 'monitor.json', snapshot('running'))
    monitor_error = initial_error or monitor_error
    while True:
        try:
            exit_code = process.wait(timeout=POLL_SECONDS)
            break
        except subprocess.TimeoutExpired:
            pass
        except KeyboardInterrupt:
            stop_requested = True
            print('STOP requested: recording the active Workbench solve before stopping.', flush=True)
        current_stage = _stage(folder)
        if current_stage != stage:
            stages.append({'stage': stage, 'seconds': time.monotonic() - phase_start})
            stage, phase_start, stage_started = current_stage, time.monotonic(), utc_now()
            limit, limit_source = warning_limit(folder, stage, case_signature)
        observed = []
        for name in ('workbench.log', 'workbench_progress.json', 'mechanical_verified.json'):
            path = folder / name
            try:
                stat = path.stat()
                observed.append((name, stat.st_mtime_ns, stat.st_size))
            except OSError:
                pass
        if observed != signatures:
            last_progress, last_progress_utc, signatures = time.monotonic(), utc_now(), observed
        status = 'delayed' if time.monotonic() - phase_start > limit else 'running'
        error = _write_diagnostic(folder / 'monitor.json', snapshot(status))
        monitor_error = error or monitor_error
    stages.append({'stage': stage, 'seconds': time.monotonic() - phase_start})
    error = _write_diagnostic(folder / 'process.json', dict(owner, state='exited', exit_code=exit_code, ended_utc=utc_now()))
    monitor_error = error or monitor_error
    _write_diagnostic(folder / 'monitor.json', snapshot('verifying' if exit_code == 0 else 'failed'))
    return exit_code, stop_requested, time.monotonic() - start


def _write_diagnostic(path, value):
    try:
        write_json(path, value)
    except OSError as error:
        return str(error)
    return None


def update_monitor(folder, **updates):
    path = Path(folder) / 'monitor.json'
    return _write_diagnostic(path, dict(read_optional(path), **updates))


def inspect_monitor(folder):
    folder = Path(folder)
    result = read_optional(folder / 'monitor.json')
    if not result:
        return {'status': 'unavailable', 'message': '此评价尚无阶段监控记录。'}
    if result.get('status') in ('running', 'delayed'):
        owner = result.get('process', {})
        actual = process_identity(owner.get('pid'))
        if actual and (not actual.get('alive') or type(owner.get('created_ticks')) is int
                       and actual.get('created_ticks') != owner['created_ticks']):
            result.update(status='interrupted', message='监控记录对应的进程已退出，请核对结果并续算。')
    return result
