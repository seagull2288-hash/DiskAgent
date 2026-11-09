"""Read-only analysis of committed Workbench observations and original cost logs.

This module never contacts a provider, launches Workbench, or changes a run.
Recorded stress improvements are not material-strength or service-life approvals.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from python_control import controller, runtime

from .schemas import TaskSpec, TaskValidationError, ToolResult
from .tools import _snapshot_data
from .validation import read_checkpoint, resolve_run


_RULES = {
    'nine variable bounds': 'bounds',
    'four decimal numerical quantization': 'precision',
    'normalized-space duplicate exclusion': 'distinct',
}
_SELECTION_ROLES = {'llm_center': 'llm_proposal', 'best_local': 'local_refinement',
                    'global_uniform': 'global_exploration'}


def _selection_metadata(proposal):
    """Read program provenance, never infer it from an LLM's exploit label."""
    if not isinstance(proposal, dict) or proposal.get('selection_policy') not in ('hybrid_local_v2', 'hybrid_local_v3'):
        return {}
    origin = proposal.get('selection_origin')
    radius = proposal.get('local_radius_fraction')
    if (not isinstance(origin, str) or origin not in _SELECTION_ROLES or
            proposal.get('selection_role') != _SELECTION_ROLES[origin] or
            type(radius) not in (int, float) or not math.isfinite(radius) or not 0 < radius <= 1):
        raise TaskValidationError('SELECTION_METADATA', 'Committed v2 selection provenance is invalid.')
    return {name: proposal[name] for name in
            ('selection_policy', 'selection_origin', 'selection_role', 'local_radius_fraction')}


def _selection_counts(samples):
    counts = dict.fromkeys(_SELECTION_ROLES, 0)
    unknown = 0
    for point in samples:
        origin = point.get('selection_origin')
        if point.get('selection_policy') in ('hybrid_local_v2', 'hybrid_local_v3') and origin in counts:
            counts[origin] += 1
        else:
            unknown += 1
    return {'origin_counts': counts, 'unknown_candidates': unknown, 'committed_candidates': len(samples)}


def _warn(warnings, message):
    message = runtime.safe_text(message)
    if message not in warnings and len(warnings) < 40:
        warnings.append(message)


def _finite(value, name):
    return runtime.bridge.finite_number(value, name)


def _percent(delta, reference, name):
    value = None if reference == 0 else _finite(delta / reference * 100.0, name)
    return 0.0 if value == 0 else value


def _inside(path, root):
    return path.resolve().is_relative_to(root.resolve())


def _read_log(path, root, warnings):
    if not _inside(path, root):
        _warn(warnings, '日志链接越出本运行目录，未读取：' + str(path.relative_to(root)))
        return None, False
    try:
        return runtime.read_json(path), True
    except Exception:
        _warn(warnings, '日志缺失或损坏，成本覆盖不完整：' + str(path.relative_to(root)))
        return None, False


def _tokens(usage, warnings, label):
    if not isinstance(usage, dict):
        return None
    result = {}
    for target, aliases in (
        ('input_tokens', ('prompt_tokens', 'input_tokens')),
        ('output_tokens', ('completion_tokens', 'output_tokens')),
        ('total_tokens', ('total_tokens',)),
    ):
        present = [usage[name] for name in aliases if name in usage]
        valid = [value for value in present if type(value) is int and value >= 0]
        result[target] = valid[0] if valid else None
        if present and (len(valid) != len(present) or len(set(valid)) > 1):
            _warn(warnings, 'usage 数值无效或别名不一致：' + label)
    if all(value is None for value in result.values()):
        return None
    if result['input_tokens'] is not None and result['output_tokens'] is not None:
        combined = result['input_tokens'] + result['output_tokens']
        if result['total_tokens'] is None:
            result['total_tokens'] = combined
        elif result['total_tokens'] != combined:
            _warn(warnings, 'usage total_tokens 与输入输出之和不一致，保留供应商值：' + label)
    return result


def _llm_costs(root, warnings):
    # Only original call directories. Iteration-root response/answer copies are excluded.
    initial_warnings = len(warnings)
    directories = set()
    for pattern in ('search/generation_*/center_api/request_*/proposal_*',
                    'iterations/iter_*/api/request_*/proposal_*'):
        for folder in root.glob(pattern):
            if folder.is_dir():
                if _inside(folder, root):
                    directories.add(folder.resolve())
                else:
                    _warn(warnings, '跳过越界请求目录：' + str(folder.relative_to(root)))
    totals = {name: 0 for name in ('input_tokens', 'output_tokens', 'total_tokens')}
    field_coverage = dict.fromkeys(totals, 0)
    attempts = successes = failure_events = unknown_deliveries = 0
    transport_coverage = usage_count = missing_usage = 0
    terminal_missing = 0
    api_seconds = []
    for folder in sorted(directories):
        label = str(folder.relative_to(root.resolve()))
        transport, valid_transport = _read_log(folder / 'transport.json', root, warnings)
        if valid_transport and isinstance(transport, list):
            transport_coverage += 1
            starts = [event for event in transport if isinstance(event, dict) and event.get('status') == 'started']
            attempts += len(starts)
            success_events = [event for event in transport if isinstance(event, dict) and event.get('status') == 'success']
            successes += len(success_events)
            failed = [event for event in transport if isinstance(event, dict) and event.get('status') in (
                'http_error', 'network_error_unknown_delivery')]
            failure_events += len(failed)
            unknown_deliveries += sum(event.get('status') == 'network_error_unknown_delivery' for event in failed)
            if (not starts or len(starts) != len(success_events) + len(failed)
                    or any(not isinstance(event, dict) for event in transport)):
                terminal_missing += 1
                _warn(warnings, 'HTTP 开始与结束记录不完整：' + label)
            for event in success_events:
                elapsed = event.get('elapsed_seconds')
                if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0:
                    api_seconds.append(float(elapsed))
        else:
            _warn(warnings, 'transport.json 不是有效事件列表：' + label)
        response, response_ok = _read_log(folder / 'response.json', root, warnings)
        usage = _tokens(response.get('usage') if isinstance(response, dict) else None, warnings, label)
        if usage is None:
            answer, answer_ok = _read_log(folder / 'answer.json', root, warnings)
            metadata = answer.get('metadata') if isinstance(answer, dict) else None
            usage = _tokens(metadata.get('usage') if isinstance(metadata, dict) else None, warnings, label)
            if usage is not None:
                _warn(warnings, '成本使用 answer.metadata.usage 备份，未重复相加：' + label)
        if usage is None:
            missing_usage += 1
            _warn(warnings, '该请求没有可用 usage，不能视为零 token 或零费用：' + label)
        else:
            usage_count += 1
            for name, value in usage.items():
                if value is not None:
                    totals[name] += value
                    field_coverage[name] += 1
    known = {name: totals[name] if field_coverage[name] else None for name in totals}
    complete = (bool(directories) and transport_coverage == len(directories)
                and usage_count == len(directories) and not failure_events and not terminal_missing
                and all(field_coverage[name] == len(directories) for name in totals)
                and len(warnings) == initial_warnings)
    accounting = 'complete' if complete else ('partial' if usage_count else 'unknown')
    if not directories:
        _warn(warnings, '未发现原始模型请求日志，无法据此认定 API/token/费用为零。')
    return {
        'scope': 'original_optimizer_request_logs_only; planner_or_supervisor_calls_not_included',
        'request_log_count': len(directories),
        'observed_http_attempts': attempts if transport_coverage else None,
        'http_transport_log_coverage': transport_coverage,
        'observed_http_successes': successes if transport_coverage else None,
        'observed_http_failure_events': failure_events if transport_coverage else None,
        'unknown_delivery_events': unknown_deliveries if transport_coverage else None,
        'usage_requests': usage_count, 'missing_usage_requests': missing_usage,
        'token_accounting_status': accounting, **known,
        'token_field_coverage': field_coverage,
        'token_details_added_separately': False,
        'observed_api_elapsed_seconds': _finite(sum(api_seconds), 'api elapsed seconds') if api_seconds else None,
        'monetary_cost': None,
        'monetary_cost_status': 'unknown; provider billing and prices not calculated',
    }


def _workbench_costs(root, state, warnings):
    started, successful = set(), set()
    for name, destination in (('evaluation_request.json', started), ('evaluation.json', successful)):
        for file in root.glob('evaluations/eval_*/' + name):
            value, valid = _read_log(file, root, warnings)
            if valid and isinstance(value, dict):
                destination.add(file.parent.resolve())
            else:
                _warn(warnings, 'Workbench 评价日志无法计数：' + str(file.relative_to(root)))
    details = ([state['baseline']] if state['baseline'] is not None else [])
    details += [record['detail'] for record in state['records']]
    solver_seconds = []
    for detail in details:
        elapsed = detail.get('elapsedSeconds')
        if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0:
            solver_seconds.append(float(elapsed))
    rounds = [record.get('elapsedSeconds') for record in state['records']]
    valid_rounds = [float(value) for value in rounds if type(value) in (int, float) and math.isfinite(value) and value >= 0]
    if details and len(solver_seconds) != len(details):
        _warn(warnings, '部分已提交 Workbench 评价缺少有效耗时，所列耗时仅为已知部分。')
    if details and (not started or not successful):
        _warn(warnings, '部分 Workbench 文件日志缺失，文件计数不代表全部实际执行次数。')
    return {
        'unit': 'Workbench评价任务，不等同底层求解器实际求解次数',
        'committed_evaluation_tasks': len(details),
        'committed_baseline_tasks': int(state['baseline'] is not None),
        'committed_candidate_tasks': len(state['records']),
        'started_evaluation_tasks': len(started),
        'successful_evaluation_files': len(successful),
        'file_counts_are_observed_only': True,
        'recorded_committed_solver_seconds': _finite(sum(solver_seconds), 'solver seconds') if solver_seconds else None,
        'solver_elapsed_coverage': len(solver_seconds),
        'recorded_candidate_round_seconds': _finite(sum(valid_rounds), 'round seconds') if valid_rounds else None,
        'candidate_round_elapsed_coverage': len(valid_rounds),
        'workflow_wall_seconds': None,
        'workflow_wall_seconds_status': 'unknown; do not add overlapping API/solver/round timing',
    }


def _observation(iteration, point, baseline, detail, prior_best, *, proposal=None):
    stress = _finite(point['stress_mpa'], 'stress_mpa')
    displacement = _finite(point['displacement_mm'], 'displacement_mm')
    area = _finite(point['area_m2'], 'area_m2')
    stress_delta = _finite(stress - baseline['stress_mpa'], 'stress delta')
    displacement_delta = _finite(displacement - baseline['displacement_mm'], 'displacement delta')
    area_delta = _finite(area - baseline['area_m2'], 'area delta')
    improvement = _finite(max(0.0, prior_best - stress), 'best improvement')
    return {
        'iteration': iteration, 'parameters': dict(point['parameters']),
        **{metric: point[metric] for metric in ('mass_kg', 'frequency_hz', 'modal_frequencies_hz') if metric in point},
        'stress_mpa': stress, 'displacement_mm': displacement, 'approximate_area_m2': area,
        'stress_change_from_baseline_mpa': stress_delta,
        'stress_change_from_baseline_percent': _percent(stress_delta, baseline['stress_mpa'], 'stress change percent'),
        'stress_reduction_from_baseline_percent': _percent(-stress_delta, baseline['stress_mpa'], 'stress reduction percent'),
        'displacement_change_from_baseline_mm': displacement_delta,
        'displacement_change_from_baseline_percent': _percent(displacement_delta, baseline['displacement_mm'], 'displacement change percent'),
        'approximate_area_change_from_baseline_m2': area_delta,
        'approximate_area_change_from_baseline_percent': _percent(area_delta, baseline['area_m2'], 'area change percent'),
        'improved_historical_best': improvement > 0,
        'historical_best_improvement_mpa': improvement,
        'historical_best_improvement_percent': _percent(improvement, prior_best, 'best improvement percent'),
        'native_report_file': detail.get('nativeReportFile'),
        'evaluation_directory': detail.get('runDir'),
        'result_source': 'committed_workbench_observation',
        **_selection_metadata(proposal),
    }


def _constraints(problem, history):
    vectors = [[point['parameters'][name] for name in problem['input_order']] for point in history]
    distinct = all(runtime.bridge.normalized_distance2(x, y, problem) > problem['min_normalized_distance']**2
                   for i, x in enumerate(vectors) for y in vectors[:i])
    rules = []
    for name in problem.get('constraints_currently_implemented', []):
        status = ('unknown' if name not in _RULES else
                  'not_evaluated' if not history else
                  'failed' if _RULES[name] == 'distinct' and not distinct else 'passed')
        rules.append({'rule': name, 'status': status})
    missing = problem.get('engineering_context', {}).get('not_implemented_constraints', [])
    return {
        'implemented_rules': rules,
        'all_implemented_rules_passed': all(rule['status'] == 'passed' for rule in rules) if rules and history else None,
        'engineering_constraints': [{'name': name, 'status': 'unknown', 'reason': 'not_implemented_or_not_configured'} for name in missing],
        'allowable_stress_status': 'unknown', 'mass_status': 'unknown', 'life_status': 'unknown',
        'engineering_qualification': 'not_assessed',
        'area_note_zh': '面积是参数公式给出的近似截面积，不等于轮盘质量。',
        'qualification_note_zh': '当前案例仅最小化最大等效应力。应力降低不等于已满足强度或寿命要求。',
    }


def analyze_run(run_id=None, *, settings=None, since_candidate=0, verify_native=False) -> ToolResult:
    """Analyze committed records; since_candidate selects a feedback window, not new work."""
    warnings = []
    root = None
    try:
        task = {'operation': 'status'}
        if run_id is not None:
            task['run_id'] = run_id
        TaskSpec.from_dict(task)
        if type(since_candidate) is not int or since_candidate < 0:
            raise TaskValidationError('ANALYSIS_WINDOW', 'since_candidate must be a nonnegative integer, not a boolean.')
        if type(verify_native) is not bool:
            raise TaskValidationError('VERIFY_FLAG', 'verify_native must be a boolean.')
        current_settings = settings if settings is not None else runtime.load_settings()
        root = resolve_run(current_settings, run_id)
        state = read_checkpoint(root)
        done = state['nCompleted']
        if since_candidate > done:
            raise TaskValidationError('ANALYSIS_WINDOW', 'since_candidate exceeds completed candidates.')
        snapshot = _snapshot_data(root)
        if snapshot['completed_candidates'] != done or snapshot['baseline_evaluations_completed'] != int(state['baseline'] is not None):
            raise TaskValidationError('RUN_CHANGED', 'Committed results changed during analysis; retry the read.')
        problem = runtime.bridge.load_problem(root / 'problem.json')
        history = state['history']
        samples = []
        prior_best = history[0]['stress_mpa'] if history else None
        evidence_records = []
        for i, point in enumerate(history):
            detail = state['baseline'] if i == 0 else state['records'][i-1]['detail']
            if verify_native:
                for key in ('nativeReportFile', 'runDir'):
                    declared = detail.get(key)
                    if not isinstance(declared, str) or not _inside(Path(declared), root):
                        raise TaskValidationError('EVIDENCE_PATH', 'Native evidence must stay within this run.')
                controller.verify_native(detail, state['baseline'] if i else None)
            samples.append(_observation(i, point, history[0], detail, prior_best,
                                        proposal=state['records'][i-1].get('proposal') if i else None))
            prior_best = min(prior_best, point['stress_mpa'])
            evidence_records.append({'iteration': i, 'native_report_file': detail.get('nativeReportFile'),
                                     'evaluation_directory': detail.get('runDir')})
        baseline = samples[0] if samples else None
        best = min(samples, key=lambda item: item['stress_mpa']) if samples else None
        current = samples[-1] if samples else None
        candidates = samples[1:]
        window_samples = candidates[since_candidate:]
        before = min(samples[:since_candidate+1], key=lambda item: item['stress_mpa']) if samples else None
        improvement = _finite(before['stress_mpa']-best['stress_mpa'], 'window improvement') if before else None
        window = {'since_candidate': since_candidate, 'completed_candidates_in_window': len(window_samples),
                  'candidate_iterations': [item['iteration'] for item in window_samples],
                  'best_before_iteration': before['iteration'] if before else None,
                  'best_before_stress_mpa': before['stress_mpa'] if before else None,
                  'best_after_stress_mpa': best['stress_mpa'] if best else None,
                  'historical_best_improvement_mpa': improvement,
                  'historical_best_improvement_percent': _percent(improvement, before['stress_mpa'], 'window best percent') if before else None,
                  'no_new_candidates': not window_samples}
        constraints = _constraints(problem, history)
        if constraints['all_implemented_rules_passed'] is False:
            _warn(warnings, '部分已实现搜索规则检查未通过或无法判断，请核对记录后再使用推荐。')
        costs = {'llm': _llm_costs(root, warnings), 'workbench': _workbench_costs(root, state, warnings)}
        feedback = {'objective': problem['objective'], 'completed_candidates': done,
                    'window_completed_candidates': len(window_samples),
                    'best_stress_mpa': best['stress_mpa'] if best else None,
                    'current_stress_mpa': current['stress_mpa'] if current else None,
                    'best_reduction_from_baseline_percent': best['stress_reduction_from_baseline_percent'] if best else None,
                    'window_best_improvement_mpa': improvement,
                    'recent_measured': [{'iteration': item['iteration'], 'stress_mpa': item['stress_mpa'],
                                         'displacement_mm': item['displacement_mm']} for item in window_samples[-3:]],
                    'causal_attribution': 'not_established', 'hotspot_attribution': 'not_available',
                    'engineering_qualification': 'not_assessed'}
        summary = (f'已有{done}个候选完成并提交，另有1个基准。最小应力方案为第{best["iteration"]}条记录，'
                   f'最大等效应力{best["stress_mpa"]:.6f} MPa，相对基准降低'
                   f'{best["stress_reduction_from_baseline_percent"]:.4f}%。'
                   f'本分析窗口包含{len(window_samples)}个新提交候选。' if best else
                   '尚无已提交基准或候选，不能给出最优应力或改进率。')
        evidence = {'basis': 'committed_controller_checkpoint',
                    'native_records_reverified': verify_native and bool(history),
                    'native_records_checked': len(history) if verify_native else 0,
                    'native_verification_status': ('passed' if history else 'no_committed_records') if verify_native else 'not_reverified',
                    'checkpoint_file': str(root/'checkpoint.json'), 'problem_file': str(root/'problem.json'),
                    'records': evidence_records, 'physical_accuracy_certified': False}
        data = {'run_id': root.name, 'run_dir': str(root), 'run_status': snapshot['run_status'],
                'last_error': snapshot.get('last_error'),
                'completed_candidates': done, 'target_total_candidates': snapshot['target_total_candidates'],
                'baseline': baseline, 'best': best, 'minimum_mass': snapshot['minimum_mass'],
                'current': current, 'window': window,
                'candidates': candidates, 'constraint_evaluation': constraints,
                'search_selection_summary': dict(_selection_counts(candidates), window=_selection_counts(window_samples)),
                'evidence': evidence, 'costs': costs, 'feedback': feedback, 'summary_zh': summary}
        if state.get('optimization_policy') is not None:
            from python_control.objectives import archive
            data['optimization'] = archive(history, state['optimization_policy'])
            data['optimization']['run_id'] = root.name
            data['optimization']['observations'] = [dict(point, iteration=i) for i, point in enumerate(history)]
            feedback['optimization'] = data['optimization']
        data['design_selection'] = controller.selection_summary(history, data.get('optimization'))
        selected = data['design_selection']['recommended']
        if data.get('optimization') is not None:
            data['summary_zh'] += (f'当前配置推荐第{selected["iteration"]}条记录。' if selected is not None else
                                   '当前配置下暂无可行推荐方案。')
        if read_checkpoint(root) != state:
            raise TaskValidationError('RUN_CHANGED','Checkpoint changed during analysis; retry the read.')
        return ToolResult(True, 'analyze', 'analyzed', data, warnings=warnings)
    except Exception as exc:
        data = {'run_id': root.name, 'run_dir': str(root)} if root is not None else {}
        return ToolResult(False, 'analyze', 'failed', data,
                          {'code': str(getattr(exc, 'code', type(exc).__name__)), 'message': runtime.safe_text(exc)}, warnings)


def _format(value, digits=6):
    return '未知/不适用' if value is None else f'{value:.{digits}f}'


def _selection_report(selections):
    statuses = {'feasible': '满足当前配置', 'infeasible': '超限', 'missing': '数据缺失'}
    source = '运行保存的目标与约束' if selections['policy_source'] == 'saved_optimization_policy' else '历史默认应力目标'
    lines = ['## 方案推荐与取舍', '', '配置版本 `' + selections['policy_id'] + '`。', '',
             '配置来源为' + source + '，已启用约束 ' + str(selections['constraint_count']) + ' 项。', '',
             '| 方案角色 | 记录 | 应力 MPa | 整盘质量 kg | 指定频率 Hz | 当前配置状态 | 归一化评分 |',
             '|---|---:|---:|---:|---:|---|---:|']
    designs = [('最小应力方案', selections['minimum_stress']), ('当前配置推荐方案', selections['recommended'])]
    designs.extend(('Pareto 方案', design) for design in selections['pareto'])
    for label, design in designs:
        if design is None:
            lines.append('| ' + label + ' | 暂无 | — | — | — | 暂无可行推荐或已提交数据 | — |')
            continue
        point, evaluation = design['observation'], design['evaluation']
        lines.append('| ' + label + ' | ' + str(design['iteration']) + ' | ' + _format(point['stress_mpa'])
                     + ' | ' + _format(point.get('mass_kg')) + ' | ' + _format(point.get('frequency_hz'))
                     + ' | ' + statuses[evaluation['status']] + ' | ' + _format(evaluation['score']) + ' |')
    if selections['constraint_count'] == 0:
        lines += ['', '当前配置未启用数值约束，方案按已选目标排序。']
    else:
        lines += ['', '### 所列方案的约束判定', '',
                  '裕量以指标原单位表示，正值表示尚有余量，负值表示超限，零表示边界。频带裕量取到最近边界的距离，频带内部为负值。', '',
                  '| 记录 | 指标 | 规则 | 实际值 | 裕量 | 状态 | 依据 |', '|---:|---|---|---:|---:|---|---|']
        seen = set()
        for _, design in designs:
            if design is None or design['iteration'] in seen:
                continue
            seen.add(design['iteration'])
            for check in design['evaluation']['constraint_checks']:
                limit = _format(check['limit'])
                if check['operator'] == 'outside_band':
                    rule = '避让 (' + limit + ', ' + _format(check['upper_limit']) + ')'
                else:
                    rule = check['operator'] + ' ' + limit
                source = check['source'].replace('|', '\\|').replace('\r', ' ').replace('\n', ' ')
                lines.append('| ' + str(design['iteration']) + ' | ' + check['metric'] + ' | ' + rule
                             + ' ' + check['margin_unit'] + ' | ' + _format(check['actual'])
                             + ' | ' + _format(check['margin']) + ' ' + check['margin_unit']
                             + ' | ' + {'passed': '通过', 'failed': '超限', 'missing': '数据缺失'}[check['status']]
                             + ' | ' + source + ' |')
    recommended = selections['recommended']
    if recommended is not None:
        lines += ['', '### 当前配置推荐方案尺寸', '',
                  '记录 ' + str(recommended['iteration']) + '，尺寸沿用原模型数值约定。', '',
                  '| 参数 | 尺寸 mm |', '|---|---:|']
        for name in runtime.bridge.NAMES:
            lines.append('| ' + name + ' | ' + _format(recommended['observation']['parameters'][name], 4) + ' |')
    return lines + ['']


def render_report(data: dict) -> str:
    """Render only the whitelisted numeric analysis; does not generate or save a file."""
    if not isinstance(data, dict) or 'run_id' not in data or 'costs' not in data:
        raise TaskValidationError('REPORT_DATA', 'render_report expects successful analyze_run.data.')
    lines = ['# 二维轮盘优化结果', '',
             f'运行：`{data["run_id"]}`；状态：`{data["run_status"]}`。', '',
             data['summary_zh'], '',
             ('本任务按保存的目标与约束配置评价，目标推荐和Pareto方案见配置评价部分。'
              if data.get('optimization') is not None else
              '当前案例仅优化最大等效应力。面积为公式近似截面积，不等于质量；应力降低不是强度或寿命合格证明。'), '']
    if data.get('design_selection') is not None:
        lines += _selection_report(data['design_selection'])
    lines += ['## 应力记录对照', '',
              '| 方案 | 记录序号 | 最大等效应力 MPa | 最大位移 mm | 近似截面积 m² | 相对基准应力降低 % |',
              '|---|---:|---:|---:|---:|---:|']
    if data.get('last_error') and data['run_status'] in ('failed','interrupted'):
        # Insert before the numeric table so an old failed run is never presented
        # as a successful engineering optimization merely because analysis works.
        error = data['last_error']
        lines[4:4] = ['最近记录错误：'+str(error.get('type',''))+' — '+str(error.get('message','')), '']
    for label, key in (('基准', 'baseline'), ('最小应力方案', 'best'), ('最新已提交', 'current')):
        point = data.get(key)
        if point:
            lines.append(f'| {label} | {point["iteration"]} | {_format(point["stress_mpa"])} | '
                         f'{_format(point["displacement_mm"])} | {_format(point["approximate_area_m2"], 8)} | '
                         f'{_format(point["stress_reduction_from_baseline_percent"], 4)} |')
    best = data.get('best')
    if best is not None:
        lines += ['', '最小应力方案相对基准的位移变化 '
                  + _format(best['displacement_change_from_baseline_percent'],4)
                  + '%；公式估算截面积变化 '
                  + _format(best['approximate_area_change_from_baseline_percent'],4)+'%。', '',
                  '## 最小应力方案尺寸', '', '| 参数 | 原模型尺寸值（mm） |', '|---|---:|']
        for name in runtime.bridge.NAMES:
            lines.append('| '+name+' | '+_format(best['parameters'][name],4)+' |')
    lines += ['', '## 分析窗口', '',
              f'第{data["window"]["since_candidate"]}个候选之后新增'
              f'{data["window"]["completed_candidates_in_window"]}个已提交候选。'
              f'历史最优应力改善 {_format(data["window"]["historical_best_improvement_mpa"])} MPa。', '',
              '## 约束与证据', '']
    for rule in data['constraint_evaluation']['implemented_rules']:
        lines.append(f'- 已实现规则 `{rule["rule"]}`：`{rule["status"]}`。')
    configured = {rule['metric'] for rule in data.get('optimization', {}).get('policy', {}).get('constraints', [])}
    engineering_metrics = {'allowable stress': 'stress_mpa', 'mass bound': 'mass_kg'}
    for rule in data['constraint_evaluation']['engineering_constraints']:
        if engineering_metrics.get(rule['name']) in configured:
            lines.append('- `' + rule['name'] + '` 的数值筛选按上述配置评价，工程限值认证状态为未知。')
        else:
            lines.append(f'- 工程约束 `{rule["name"]}`：未知，当前未实现或未配置。')
    lines += ['- 强度判定、质量约束与寿命判定均未认证。',
              '- 不根据参数变化推断已验证的热点位置或物理因果关系。',
              f'- 原生记录重新核验：`{data["evidence"]["native_verification_status"]}`。',
              f'- 源断点：`{data["evidence"]["checkpoint_file"]}`。', '', '## 计算与模型调用记录', '']
    llm, wb = data['costs']['llm'], data['costs']['workbench']
    def count(value):
        return '未知' if value is None else str(value)
    lines += [f'- 已提交 Workbench 评价任务：{wb["committed_evaluation_tasks"]}，'
              f'其中基准{wb["committed_baseline_tasks"]}、候选{wb["committed_candidate_tasks"]}。',
              f'- 文件可观察到开始的评价任务：{wb["started_evaluation_tasks"]}，'
              f'成功结果文件：{wb["successful_evaluation_files"]}。文件计数可能不完整。',
              '- 上述是评价任务数，不等同求解器内部实际求解次数。',
              f'- 原始优化器请求目录：{llm["request_log_count"]}；'
              f'可观察 HTTP 尝试：{count(llm["observed_http_attempts"])}。',
              f'- Token统计状态：`{llm["token_accounting_status"]}`；'
              f'输入{count(llm["input_tokens"])}、输出{count(llm["output_tokens"])}、合计{count(llm["total_tokens"])}。',
              '- Token只统计原始优化器请求，未叠加迭代目录复制件或usage细分项，也不包含规划器/调度器自身调用。',
              '- 缺失usage、失败或损坏日志代表成本未知或部分已知，不能视为零花费。货币费用尚未计算。', '']
    selection = data.get('search_selection_summary')
    if isinstance(selection, dict):
        counts = selection['origin_counts']
        lines += ['## 实际选点来源（仅已提交候选）', '',
                  '- LLM中心直评：'+str(counts['llm_center'])+'；历史最优附近局部细化：'
                  +str(counts['best_local'])+'；全局均匀探索：'+str(counts['global_uniform'])+'。',
                  '- 来源未记录：'+str(selection['unknown_candidates'])+'。'
                  '旧记录不按模型的exploit/explore声明推断程序实际选点方式。', '']
    optimization = data.get('optimization')
    if optimization is not None:
        lines += ['', '## 配置目标与可行方案', '',
                  '配置版本 ' + optimization['policy_id'],
                  '可行记录数 ' + str(optimization['feasible_count']),
                  '目标推荐记录 ' + str(optimization['best_iteration']),
                  'Pareto记录 ' + ', '.join(map(str, optimization['pareto_iterations'])), '',
                  '| 记录 | 应力 MPa | 整盘质量 kg | 指定频率 Hz | 可行状态 | 归一化评分 |',
                  '|---|---:|---:|---:|---|---:|']
        for point, evaluation in zip(optimization['observations'], optimization['evaluations']):
            lines.append('| ' + str(point['iteration']) + ' | ' + _format(point['stress_mpa']) + ' | '
                         + _format(point.get('mass_kg')) + ' | ' + _format(point.get('frequency_hz')) + ' | '
                         + evaluation['status'] + ' | ' + _format(evaluation['score']) + ' |')
        lines += ['', '### 已配置约束依据', '']
        for rule in optimization['policy']['constraints']:
            lines.append('- ' + rule['metric'] + ' ' + rule['operator'] + ' ' + str(rule['value'])
                         + ' / ' + rule['reference'] + '，依据 ' + rule['source'])
        if optimization['policy']['modal'] is not None:
            lines += ['', '模态按配置的阶次取值，跨阶振型跟踪尚待独立验证。']
    modal_points = [point for point in [data.get('baseline')] + data.get('candidates', []) if point]
    if any('modal_frequencies_hz' in point for point in modal_points):
        lines += ['', '## 六阶预应力模态频率', '',
                  '阶次按当前求解结果排序，工况与原生证据随各记录保存。', '',
                  '| 记录 | f1 Hz | f2 Hz | f3 Hz | f4 Hz | f5 Hz | f6 Hz |',
                  '|---:|---:|---:|---:|---:|---:|---:|']
        for point in modal_points:
            values = point.get('modal_frequencies_hz', [None] * 6)
            lines.append('| ' + str(point['iteration']) + ' | ' + ' | '.join(_format(value) for value in values) + ' |')
    return '\n'.join(lines)
