"""Launch the existing RunWB2 macro and accept only verified native results."""
from __future__ import annotations

import csv
from datetime import datetime
import io
import math
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .runtime import ControllerError, bridge, read_json, write_json
from .journal import POLICY, create as create_journal


def canonical_x(x, problem):
    if not isinstance(x, (list, tuple)) or len(x) != 9:
        raise ControllerError('A design must contain exactly nine finite numbers.')
    return bridge.ordered_values(dict(zip(bridge.NAMES, x)), problem)


def approximate_area(x):
    a, b, c, d, e, f, g, h, i = x
    return (2*a*f + b*(0.5339554795363548*f + 1.4660445204636461*g)
            + c*(g+h) + d*(1.422837979365895*h + 0.57716202063410482*i) + 2*e*i)*1e-6


def project_unlocked(project):
    lock = project.parent / (project.stem + '_files') / '.lock'
    if lock.exists():
        raise ControllerError('Workbench project is locked. Close its open session or wait for the active solve: ' + str(lock))


def read_workbench_csv(path, expected, decimals):
    raw = Path(path).read_bytes()
    if not raw or b'\0' in raw:
        raise ControllerError('Workbench CSV is empty or contains unsupported NUL bytes.')
    # P headers/numeric rows are ASCII; preserve 8-bit localized metadata losslessly.
    text = raw.removeprefix(b'\xef\xbb\xbf').decode('latin-1')
    required = ['P' + str(i) for i in range(1, 13)]
    header = None
    points = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        try:
            fields = [value.strip() for value in next(csv.reader([line], strict=True))]
        except csv.Error as exc:
            raise ControllerError('Malformed Workbench CSV.') from exc
        if header is None:
            upper = [f.upper() for f in fields]
            if all(upper.count(name) == 1 for name in required):
                header = [upper.index(name) for name in required]
                if 0 in header:
                    raise ControllerError('Expected the design-point name in the first CSV column.')
            continue
        if ''.join(fields[0].upper().split()) == 'DP0':
            if len(fields) <= max(header):
                raise ControllerError('Workbench DP0 row is missing columns.')
            points.append([fields[index] for index in header])
    if header is None or len(points) != 1:
        raise ControllerError('Expected unique P1..P12 columns and exactly one DP0 row.')
    values = []
    for index, value in enumerate(points[0]):
        try:
            number = float(value)
        except ValueError:
            number = None
        if number is not None and not math.isfinite(number):
            number = None
        if index not in (9, 10) and number is None:
            raise ControllerError('Missing/nonfinite Workbench input or area value.')
        values.append(number)
    actual = [values[i] for i in (7, 8, 0, 1, 2, 3, 4, 5, 6)]
    tolerance = 0.51 * 10**(-decimals)
    if any(abs(a-b) > tolerance + 16*math.ulp(max(1, abs(a), abs(b))) for a, b in zip(actual, expected)):
        raise ControllerError('Exported dimensions differ from this candidate; refusing a stale/wrong result.')
    if values[11] <= 0:
        raise ControllerError('Section area must be positive.')
    return {'xActual': actual, 'area': values[11], 'pValues': values,
            'csvFile': str(path), 'workbenchCachedStressMPa': values[9],
            'workbenchCachedDisplacementMM': values[10]}


def verify_native(detail, baseline=None):
    native = read_json(detail['nativeReportFile'])
    actual = native.get('actual', {})
    fresh = actual.get('result_file_after', {}).get('fresh_for_this_solve')
    reused = actual.get('result_source') == 'workbench_update'
    if reused:
        verify_reused_update(native, detail['nativeReportFile'])
    if native.get('success') is not True or fresh is not True or (not reused and native.get('solve_requested') is not True):
        raise ControllerError('A successful fresh native Mechanical solve is required.')
    for native_key, detail_key in (('stress_MPa', 'stressMPa'), ('displacement_mm', 'displacementMM')):
        value = bridge.finite_number(native.get(native_key), native_key)
        if value < 0 or abs(value-detail[detail_key]) > max(1e-8, abs(value)*1e-10):
            raise ControllerError('Native result and recorded result disagree: ' + native_key)
    x = detail['xActual']
    if abs(approximate_area(x)-detail['area']) > 1e-9:
        raise ControllerError('Section area does not match the actual dimensions.')
    if baseline:
        original = read_json(baseline['nativeReportFile'])['actual']
        for key in ('material', 'analysis_type', 'model_2d_behavior'):
            if actual['baseline_after'][key] != original['baseline_after'][key]:
                raise ControllerError('Physics changed from the baseline: ' + key)
        mesh, old_mesh = actual['mesh_after'], original['mesh_after']
        if (mesh['element_order'] != old_mesh['element_order']
                or abs(mesh['element_size_mm']-old_mesh['element_size_mm']) > 1e-10):
            raise ControllerError('Mesh settings changed from the baseline.')
    return native


def verify_reused_update(native, report_file):
    """Require explicit reuse evidence, including this evaluation's update marker."""
    actual = native.get('actual', {})
    result = actual.get('result_file_after', {})
    solver = actual.get('solver_output', {})
    states = actual.get('object_states_after', {})
    if (native.get('evaluator_policy') != POLICY or native.get('solve_requested') is not False
            or actual.get('extra_solve_skipped') is not True
            or result.get('fresh_for_this_update') is not True
            or actual.get('status_after') != 'Done'
            or any(states.get(name) != 'Solved' for name in ('solution', 'stress'))
            or solver.get('error_banner_count') != 0 or actual.get('new_error_messages') != []):
        raise ControllerError('Incomplete evidence for reusing this Workbench update.')
    try:
        def utc(value):
            stamp = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                raise ValueError('UTC offset is required')
            return stamp
        marker = Path(report_file).parent / 'solve_started_utc.txt'
        started = utc(marker.read_text(encoding='utf-8-sig'))
        if started != utc(actual['update_started_utc']):
            raise ValueError('Update marker disagrees with the report')
        finished = utc(native['finished_utc'])
        for snapshot in (result, solver):
            if (snapshot.get('exists') is not True or type(snapshot.get('bytes')) is not int
                    or snapshot['bytes'] <= 0
                    or not started < utc(snapshot['last_modified_utc']) <= finished):
                raise ValueError('Result/solver output is missing, empty or stale')
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ControllerError('Fresh Workbench update evidence failed: ' + str(exc)) from exc


def collect_result(run_dir, x, problem, baseline=None):
    run_dir = Path(run_dir)
    if not (run_dir / 'complete.ok').is_file() or (run_dir / 'complete.ok').read_text(encoding='ascii').strip() != 'WB2D_COMPLETE_V1':
        raise ControllerError('Workbench did not reach the verified end of this macro.')
    detail = read_workbench_csv(run_dir / 'result.csv', x, problem['decimals'])
    native_path = run_dir / 'mechanical_verified.json'
    native = read_json(native_path)
    detail.update(stressMPa=bridge.finite_number(native.get('stress_MPa'), 'stress_MPa'),
                  displacementMM=bridge.finite_number(native.get('displacement_mm'), 'displacement_mm'),
                  nativeReportFile=str(native_path), runDir=str(run_dir), xRequested=x, xWritten=x,
                  journalFile=str(run_dir / 'evaluate.wbjn'), completeFile=str(run_dir / 'complete.ok'),
                  logFile=str(run_dir / 'workbench.log'),
                  resultSource='Fresh native Mechanical solution, not Workbench cached P10/P11')
    detail['reusedWorkbenchUpdate'] = native.get('actual', {}).get('result_source') == 'workbench_update'
    detail['extraNativeSolveRequested'] = native.get('solve_requested') is True
    if detail['reusedWorkbenchUpdate']:
        detail['resultSource'] = 'Fresh Workbench update, checked and read through native Mechanical API'
    cached = detail['workbenchCachedStressMPa']
    detail['workbenchCsvAgreesWithNative'] = cached is not None and abs(cached-detail['stressMPa']) < 1e-5
    verify_native(detail, baseline)
    return detail


def evaluate(x, settings, problem, output_root, baseline=None):
    x = canonical_x(x, problem)
    if sys.platform != 'win32':
        raise ControllerError('The installed Workbench interface requires Windows.')
    project_unlocked(settings.project_file)
    output_root = Path(output_root).resolve()
    project_root = settings.project_file.parent.resolve()
    if output_root == project_root or output_root.is_relative_to(project_root) or project_root.is_relative_to(output_root):
        raise ControllerError('Evaluation folders must not overlap the Workbench project folder.')
    run_dir = output_root / ('eval_' + uuid.uuid4().hex)
    run_dir.mkdir(parents=True, exist_ok=False)
    write_json(run_dir / 'evaluation_request.json', {'x': x, 'project': str(settings.project_file)})
    create_journal(settings.root, x, run_dir / 'evaluate.wbjn', run_dir / 'result.csv', run_dir / 'complete.ok')
    project_unlocked(settings.project_file)
    command = [str(settings.workbench_executable), '-F', str(settings.project_file), '-B', '-R', str(run_dir / 'evaluate.wbjn')]
    started = time.monotonic()
    stop_requested = False
    # A dedicated process group prevents Ctrl+C from interrupting Workbench midway
    # through project updates. Finish and record this solve, then stop the loop.
    with (run_dir / 'workbench.log').open('wb') as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                   cwd=str(settings.root), creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        while True:
            try:
                exit_code = process.wait()
                break
            except KeyboardInterrupt:
                stop_requested = True
                print('STOP requested: waiting for the active Workbench solve; its result will be saved first.', flush=True)
    if exit_code:
        raise ControllerError('Workbench failed with exit code %d. Inspect %s' % (exit_code, run_dir / 'workbench.log'))
    detail = collect_result(run_dir, x, problem, baseline)
    detail.update(projectFile=str(settings.project_file), elapsedSeconds=time.monotonic()-started,
                  exitCode=exit_code, stopRequested=stop_requested)
    write_json(run_dir / 'evaluation.json', detail)
    return detail
