"""Independent mesh refinement studies over the verified static/modal evaluator."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import hashlib
import math
from pathlib import Path
import shutil
import uuid

from python_control import runtime, workbench
from python_control.controller import observation
from python_control.objectives import default_policy


def source_hashes(project):
    files = [project] + list(project.parent.rglob('*.agdb')) + list(project.parent.rglob('*.mechdb'))
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}


def default_designs(problem):
    reference = {variable['name']: variable['reference'] for variable in problem['variables']}
    candidate = dict(reference, X1=round(reference['X1'] + .2, 4))
    return [{'label': '参考设计', 'parameters': reference}, {'label': 'X1增加0.2mm的验收候选', 'parameters': candidate}]


def compare_meshes(cases, tolerance=None):
    comparisons = []
    metrics = ['stress_mpa'] + ['f%d_hz' % order for order in range(1, 7)]
    labels = list(dict.fromkeys(case['label'] for case in cases))
    for label in labels:
        points = sorted((case for case in cases if case['label'] == label), key=lambda case: -case['mesh_size_mm'])
        for metric in metrics:
            values = [case['observation']['stress_mpa'] if metric == 'stress_mpa' else
                      case['observation']['modal_frequencies_hz'][int(metric[1]) - 1] for case in points]
            differences = [abs(b - a) for a, b in zip(values, values[1:])]
            relative = [abs(b - a) / abs(b) for a, b in zip(values, values[1:])]
            comparisons.append({'label': label, 'metric': metric, 'values': values,
                'absolute_changes': differences, 'relative_changes': relative,
                'changes_decreasing': len(differences) >= 2 and differences[-1] <= differences[-2],
                'configured_tolerance': tolerance,
                'within_configured_tolerance': relative[-1] <= tolerance if tolerance is not None else None})
    return comparisons


def render_report(report):
    lines = ['# 轮盘网格细化验证', '', '状态 ' + report['status'], '',
             '网格尺寸采用 mm，静力应力采用 MPa，模态频率采用 Hz。工况沿用工作转速下的预应力轴对称模型。', '',
             '分析范围为零节径、固定阶次的前六阶模态。', '',
             '| 设计 | 网格 mm | 节点数 | 单元数 | 应力 MPa | 质量 kg | f1 Hz | f2 Hz | f3 Hz | f4 Hz | f5 Hz | f6 Hz |',
             '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for case in report['cases']:
        point = case['observation']
        values = [case['label'], str(case['mesh_size_mm']), str(case['node_count']), str(case['element_count']),
                  '%.6f' % point['stress_mpa'], '%.6f' % point['mass_kg']]
        values += ['%.6f' % value for value in point['modal_frequencies_hz']]
        lines.append('| ' + ' | '.join(values) + ' |')
    lines += ['', '## 相邻网格变化', '',
              '| 设计 | 指标 | 前两级变化 % | 后两级变化 % | 变化幅度是否缩小 |', '|---|---|---:|---:|---|']
    for item in report['comparisons']:
        changes = item['relative_changes']
        lines.append('| ' + item['label'] + ' | ' + item['metric'] + ' | '
                     + ' | '.join('%.6f' % (value * 100) for value in changes) + ' | '
                     + ('是' if item['changes_decreasing'] else '否') + ' |')
    lines += ['', '数值验收容差 ' + (str(report['relative_tolerance']) if report['relative_tolerance'] is not None else '待配置'), '',
              '每个结果对应独立工程、实际网格计数和原生结果文件。源工程哈希核对结果为 '
              + str(report['source_preserved']) + '。', '', '## 原生证据', '']
    for case in report['cases']:
        lines.append('- ' + case['label'] + '，网格 ' + str(case['mesh_size_mm']) + ' mm，记录 '
                     + str(case['native_report_file']))
    return '\n'.join(lines)


def run_study(*, settings=None, mesh_sizes=(.5, .4, .3), designs=None, relative_tolerance=None,
              live=False, study_id=None):
    settings = settings or runtime.load_settings()
    sizes = [float(value) for value in mesh_sizes]
    if len(sizes) != 3 or any(not math.isfinite(value) or value <= 0 for value in sizes) or sizes != sorted(set(sizes), reverse=True):
        raise ValueError('请选择由粗到细的三个不同正网格尺寸。')
    if relative_tolerance is not None and (not math.isfinite(relative_tolerance) or relative_tolerance <= 0):
        raise ValueError('数值验收容差应为正数，或保持待配置。')
    problem = runtime.bridge.load_problem(settings.problem_file)
    designs = [dict(design, parameters=dict(design['parameters'])) for design in (designs or default_designs(problem))]
    if not 1 <= len(designs) <= 2 or len({design['label'] for design in designs}) != len(designs):
        raise ValueError('首版网格验证支持一至两个名称不同的设计。')
    for design in designs:
        x = runtime.bridge.ordered_values(design['parameters'], problem)
        design['parameters'] = dict(zip(problem['input_order'], x))
    policy = default_policy()
    policy.update(collect_mass=True, modal={'analysis_name': 'DiskAgent Prestressed Axisymmetric',
        'mode_index': 1, 'family': 'axisymmetric_fixed_order', 'prestress_required': True})
    source_settings = settings.for_policy(policy)
    request = {'mesh_sizes_mm': sizes, 'designs': designs, 'relative_tolerance': relative_tolerance,
               'policy': policy, 'design_evaluation_tasks': len(designs) * len(sizes), 'llm_requests': 0}
    if not live:
        return dict(status='preview', request=request)
    if study_id is None:
        study_id = 'mesh_study_' + datetime.now().strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:8]
    if not study_id.startswith('mesh_study_') or any(character not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_' for character in study_id):
        raise ValueError('网格验证编号格式无效。')
    folder = settings.output_root / study_id
    if folder.resolve().parent != settings.output_root.resolve():
        raise ValueError('网格验证目录应位于运行目录。')
    with runtime.project_lock(settings.root):
        folder.mkdir(parents=True, exist_ok=True)
        manifest = folder / 'study.json'
        hashes = source_hashes(source_settings.project_file)
        report = {'study_id': study_id, 'status': 'running', 'request': request, 'cases': [],
                  'source_hashes': hashes, 'relative_tolerance': relative_tolerance}
        if manifest.is_file():
            report = runtime.read_json(manifest)
            if report['request'] != request or report['source_hashes'] != hashes:
                raise ValueError('恢复时的设计、网格或源工程与保存记录不一致。')
        runtime.write_json(manifest, report)
        runtime.write_json(settings.output_root / 'mesh_study_latest.json', {'study_id': study_id})
        try:
            for design_index, design in enumerate(designs):
                for mesh_index, size in enumerate(sizes):
                    case_id = 'case_%d_%d' % (design_index, mesh_index)
                    saved = next((case for case in report['cases'] if case['case_id'] == case_id), None)
                    if saved is not None:
                        workbench.verify_native(runtime.read_json(saved['evaluation_file']))
                        continue
                    case_root = folder / case_id
                    project = case_root / 'project' / source_settings.project_file.name
                    workbench.project_unlocked(project)
                    if not project.is_file():
                        shutil.copytree(source_settings.project_file.parent, project.parent,
                            ignore=shutil.ignore_patterns('*.rst', '*.rth', '*.full', '*.emat', '*.esav', '*.osav',
                                '*.mode', '*.rdb', '*.ldhi', '*.lck', '*.lock', '*.out', '*.err', 'session_files', '.project_cache'),
                            dirs_exist_ok=True)
                    case_settings = replace(source_settings, project_file=project)
                    x = runtime.bridge.ordered_values(design['parameters'], problem)
                    runtime.write_json(folder / 'progress.json', {'case_id': case_id, 'label': design['label'],
                                       'mesh_size_mm': size, 'completed_cases': len(report['cases'])})
                    print('MESH %s %s h=%.4g mm' % (case_id, design['label'], size), flush=True)
                    completed = list((case_root / 'evaluation').glob('eval_*/complete.ok'))
                    if len(completed) == 1:
                        detail = workbench.collect_result(completed[0].parent, x, problem)
                        runtime.write_json(completed[0].parent / 'evaluation.json', detail)
                    else:
                        detail = workbench.evaluate(x, case_settings, problem, case_root / 'evaluation', mesh_size_mm=size)
                    native = workbench.verify_native(detail)
                    mesh = native['actual']['mesh_after']
                    if abs(mesh['element_size_mm'] - size) > 1e-9:
                        raise ValueError('求解中的实际网格尺寸与该案例请求不一致。')
                    if len(detail.get('modal_frequencies_hz', [])) != 6:
                        raise ValueError('该网格案例缺少六阶模态结果。')
                    report['cases'].append({'case_id': case_id, 'label': design['label'], 'mesh_size_mm': size,
                        'observation': observation(detail), 'node_count': mesh['node_count'],
                        'element_count': mesh['element_count'], 'native_report_file': detail['nativeReportFile'],
                        'evaluation_file': str(Path(detail['runDir']) / 'evaluation.json'),
                        'metric_evidence': detail.get('metric_evidence', {})})
                    runtime.write_json(manifest, report)
            report.update(status='completed', comparisons=compare_meshes(report['cases'], relative_tolerance),
                          source_preserved=hashes == source_hashes(source_settings.project_file),
                          completed_at=runtime.local_time())
            if not report['source_preserved']:
                raise ValueError('源工程发生了变化，请核对源工程和独立副本。')
            rows = []
            for case in report['cases']:
                row = {'design': case['label'], 'mesh_mm': case['mesh_size_mm'], 'nodes': case['node_count'],
                       'elements': case['element_count'], 'stress_mpa': case['observation']['stress_mpa'],
                       'mass_kg': case['observation']['mass_kg']}
                row.update({'f%d_hz' % (i + 1): value for i, value in enumerate(case['observation']['modal_frequencies_hz'])})
                rows.append(row)
            runtime.write_csv(folder / 'mesh_results.csv', rows, list(rows[0]))
            runtime.atomic_text(folder / '网格验证报告.md', render_report(report))
            runtime.write_json(manifest, report)
            runtime.write_json(settings.output_root / 'mesh_study_latest.json', {'study_id': study_id})
            return report
        except BaseException as exc:
            report.update(status='failed', error=runtime.safe_text(exc), source_preserved=hashes == source_hashes(source_settings.project_file))
            runtime.write_json(manifest, report)
            raise


def read_latest(settings):
    pointer = settings.output_root / 'mesh_study_latest.json'
    if not pointer.is_file():
        return {'ok': True, 'data': None}
    study_id = runtime.read_json(pointer)['study_id']
    folder = settings.output_root / study_id
    if folder.resolve().parent != settings.output_root.resolve() or not study_id.startswith('mesh_study_'):
        raise ValueError('网格验证路径无效。')
    report = runtime.read_json(folder / 'study.json')
    progress = folder / 'progress.json'
    if progress.is_file():
        report['progress'] = runtime.read_json(progress)
    return {'ok': True, 'data': report}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--mesh-sizes', nargs=3, type=float, default=[.5, .4, .3])
    parser.add_argument('--designs', help='JSON list containing label and canonical parameters.')
    parser.add_argument('--relative-tolerance', type=float)
    parser.add_argument('--study-id')
    args = parser.parse_args()
    report = run_study(mesh_sizes=args.mesh_sizes, designs=runtime.read_json(args.designs) if args.designs else None,
                       relative_tolerance=args.relative_tolerance, live=args.live, study_id=args.study_id)
    print(runtime.safe_text(str({'status': report['status'], 'study_id': report.get('study_id'),
                                'cases': len(report.get('cases', []))})), flush=True)
