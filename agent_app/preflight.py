"""Local readiness checks with a disposable write probe and no API/solver calls."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import socket
import tempfile

import run_python_optimization as entry
from python_control import runtime
from python_control.workbench import project_unlocked

from .schemas import TaskValidationError


DISK_WARNING_BYTES = 5 * 1024 ** 3
LICENSE_CONNECT_TIMEOUT_SECONDS = 0.5


def _existing_parent(path):
    path = Path(path).resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    if not path.is_dir():
        raise OSError('输出路径的已有父路径不是目录。')
    return path


def _probe_write(directory):
    descriptor, filename = tempfile.mkstemp(prefix='.diskagent-preflight-', dir=directory)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(b'disk-agent-write-probe\n')
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        Path(filename).unlink(missing_ok=True)


def _license_endpoints(settings):
    configured = [os.environ.get(name, '') for name in ('ANSYSLMD_LICENSE_FILE', 'ANSYSLI_SERVERS')]
    program_files = os.environ.get('ProgramFiles')
    candidates = []
    if program_files:
        candidates.append(Path(program_files) / 'ANSYS Inc' / 'Shared Files' / 'Licensing' / 'ansyslmd.ini')
    for parent in Path(settings.workbench_executable).parents:
        if parent.name.lower() == 'ansys inc':
            candidates.append(parent / 'Shared Files' / 'Licensing' / 'ansyslmd.ini')
            break
    for path in dict.fromkeys(candidates):
        if path.is_file():
            text = path.read_text(encoding='utf-8-sig', errors='replace')
            configured.extend(line.partition('=')[2].strip() for line in text.splitlines()
                              if line.partition('=')[0].strip().upper() in ('SERVER', 'ANSYSLI_SERVERS'))
    endpoints = []
    for value in configured:
        for item in value.split(';'):
            match = re.fullmatch(r'\s*([0-9]{1,5})@([A-Za-z0-9_.-]+)\s*', item)
            if match and 0 < int(match[1]) <= 65535:
                endpoints.append((match[2], int(match[1])))
    return list(dict.fromkeys(endpoints))[:2]


def _policy_message(settings):
    policy = getattr(settings, 'optimization_policy', None)
    if policy is None:
        return '采用现有静力任务配置，质量与频率采集按该任务配置执行。'
    from python_control.objectives import validate_policy
    policy = validate_policy(policy)
    bound = settings.for_policy(policy)
    if bound.project_file.resolve() != settings.project_file.resolve():
        raise runtime.ControllerError('当前工程与所选频率任务不匹配。')
    mass = '开启' if policy['collect_mass'] else '关闭'
    if policy['modal'] is None:
        return '目标与约束配置有效，质量采集' + mass + '，频率采集关闭。'
    return ('目标与约束配置有效，质量采集' + mass + '，所选模态分析与已验证工程配置一致。'
            '模型对象的实际可用性将在原生预检阶段核验。')


def inspect_preflight(settings=None, run_id=None, *, environment_report=None):
    """Inspect configuration and storage without submitting a design or changing a checkpoint."""
    checks = []

    def add(check_id, label, status, message, **details):
        checks.append(dict(id=check_id, label=label, status=status, message=message, **details))

    try:
        current = settings if settings is not None else runtime.load_settings()
    except Exception as error:
        add('settings', '任务配置', 'failed', runtime.safe_text(error))
        return _report(checks, run_id)

    try:
        if environment_report is None:
            entry.check_environment(current, extended=False)
        add('environment', '文件、依赖与参数映射', 'passed', '现有环境检查已通过。')
    except Exception as error:
        add('environment', '文件、依赖与参数映射', 'failed', runtime.safe_text(error))

    secret = ''
    try:
        secret = runtime.saved_key(current.key_environment_variable)
        if not secret or any(char in secret for char in '\0\r\n'):
            raise runtime.ControllerError('请配置有效的单行API密钥。')
        add('api_key', 'API密钥配置', 'passed', '已找到单行密钥，服务端鉴权与余额须在实际请求时确认。')
    except Exception:
        add('api_key', 'API密钥配置', 'failed', '请在任务指定的环境变量中配置有效的单行API密钥。')

    try:
        project_unlocked(current.project_file)
        add('project_lock', 'Workbench工程占用', 'passed', '当前未发现Workbench工程锁。')
    except Exception as error:
        add('project_lock', 'Workbench工程占用', 'failed', runtime.safe_text(error))

    parent = None
    try:
        destination = Path(current.output_root).resolve()
        parent = _existing_parent(destination)
        _probe_write(parent)
        message = '输出目录的临时写入、刷新与清理检查通过。'
        if parent != destination:
            message = '输出目录尚未创建，已确认其已有父目录可写。'
        add('output_write', '输出目录写入', 'passed', message)
    except Exception as error:
        add('output_write', '输出目录写入', 'failed', runtime.safe_text(error))

    try:
        parent = parent or _existing_parent(current.output_root)
        free = shutil.disk_usage(parent).free
        status = 'failed' if free == 0 else 'warning' if free < DISK_WARNING_BYTES else 'passed'
        add('disk_space', '磁盘可用空间', status,
            '可用%.2f GiB，默认运行提醒阈值为5 GiB，实际需求取决于模型与求解规模。' % (free / 1024 ** 3),
            free_bytes=free, warning_below_bytes=DISK_WARNING_BYTES)
    except Exception as error:
        add('disk_space', '磁盘可用空间', 'unknown', '暂时无法读取磁盘空间，需检查输出盘是否可用。')

    try:
        add('analysis_policy', '质量、频率与分析配置', 'passed', _policy_message(current))
    except Exception as error:
        add('analysis_policy', '质量、频率与分析配置', 'failed', runtime.safe_text(error))

    try:
        endpoints = _license_endpoints(current)
        reached = 0
        for endpoint in endpoints:
            try:
                with socket.create_connection(endpoint, timeout=LICENSE_CONNECT_TIMEOUT_SECONDS):
                    reached += 1
            except OSError:
                pass
        if reached:
            add('license_service', '许可证服务连接', 'passed', '已配置的许可证服务端口可达。')
        elif endpoints:
            add('license_service', '许可证服务连接', 'warning', '已配置的许可证服务端口暂不可达，请检查许可服务或网络。')
        else:
            add('license_service', '许可证服务连接', 'unknown', '未找到明确的许可证服务器端点，许可连接待实际启动时确认。')
    except OSError:
        add('license_service', '许可证服务连接', 'unknown', '许可证配置暂不可读，许可连接待实际启动时确认。')
    add('license_entitlement', '实际求解许可', 'unknown', '本次体检未申请求解许可，具体功能授权与空闲许可数量待实际运行确认。')

    if run_id is not None:
        try:
            from .validation import resolve_run
            from python_control.controller import restore_state
            directory = resolve_run(current, run_id)
            restore_state(current, str(directory))
            add('resume_compatibility', '续算配置与原生证据', 'passed', '所选断点的配置、计数与已提交原生结果核验通过。')
        except Exception as error:
            add('resume_compatibility', '续算配置与原生证据', 'failed', runtime.safe_text(error))

    result = _report(checks, run_id)
    if secret:
        for check in result['checks']:
            check['message'] = check['message'].replace(secret, '[REDACTED]')
    return result


def _report(checks, run_id):
    counts = {status: sum(check['status'] == status for check in checks)
              for status in ('passed', 'warning', 'failed', 'unknown')}
    status = 'failed' if counts['failed'] else 'warning' if counts['warning'] or counts['unknown'] else 'passed'
    return {'ok': not counts['failed'], 'schema_version': 'disk-agent.preflight.v1', 'status': status,
            'run_id': run_id, 'checks': checks, 'counts': counts,
            'summary': '体检完成，通过%d项，提醒%d项，失败%d项，待运行确认%d项。' %
                       (counts['passed'], counts['warning'], counts['failed'], counts['unknown']),
            'checked_at': runtime.local_time(), 'api_requests_made': 0, 'finite_element_solves_made': 0}


def require_ready(settings, run_id=None, *, environment_report=None):
    """Block only confirmed local failures and return the same reviewable report."""
    report = inspect_preflight(settings, run_id, environment_report=environment_report)
    failed = [check for check in report['checks'] if check['status'] == 'failed']
    if failed:
        raise TaskValidationError('PREFLIGHT_FAILED', '\n'.join(check['label'] + ' · ' + check['message'] for check in failed))
    return report
