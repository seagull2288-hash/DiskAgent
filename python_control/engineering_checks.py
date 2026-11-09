"""Deterministic geometry prechecks and observational result diagnostics."""
from __future__ import annotations

import math

from .runtime import ControllerError, bridge, read_json


GEOMETRY_POLICY = 'disk_2d_geometry_precheck_v1'
RESULT_POLICY = 'disk_result_diagnostics_v1'
INNER_RADIUS_MM = 83.0
STRESS_CHANGE_WARNING_PERCENT = 25.0
PARAMETER_IDS = ['P8', 'P9', 'P1', 'P2', 'P3', 'P4', 'P5', 'P6', 'P7']


def inspect_geometry(x, problem):
    """Check implications of the registered span/half-height parameterization."""
    result = {'schema_version': GEOMETRY_POLICY, 'ok': True, 'status': 'passed',
              'source': 'registered_nine_variable_disk_and_native_geometry_verifier',
              'checks': [], 'full_geometry_certified': False}
    checks = result['checks']
    if (problem.get('input_order') != list(bridge.NAMES)
            or problem.get('workbench_parameter_ids') != PARAMETER_IDS):
        checks.append({'id': 'registered_geometry', 'label': '几何参数定义', 'status': 'unknown',
                       'message': '当前模型未注册本预检使用的五段跨度与四个半高定义。'})
        result['status'] = 'unknown'
        return result
    try:
        if not isinstance(x, (list, tuple)) or len(x) != 9:
            raise ValueError('A design requires nine dimensions.')
        values = [bridge.finite_number(value, name) for name, value in zip(bridge.NAMES, x)]
    except (ValueError, TypeError, bridge.BridgeError) as error:
        result.update(ok=False, status='failed')
        checks.append({'id': 'finite_dimensions', 'label': '几何尺寸数值', 'status': 'failed',
                       'message': str(error)})
        return result
    spans, half_heights = values[:5], values[5:]
    stations = [INNER_RADIUS_MM]
    for span in spans:
        stations.append(stations[-1] + span)
    heights = [half_heights[0], half_heights[0], half_heights[1],
               half_heights[2], half_heights[3], half_heights[3]]
    from .workbench import approximate_area
    area = approximate_area(values)
    rules = [
        ('positive_spans', '五段径向跨度', all(value > 0 for value in spans),
         '每段跨度须大于零。'),
        ('positive_half_heights', '四处截面半高', all(value > 0 for value in half_heights),
         '四个半高须大于零，保证已定义站点处截面厚度为正。'),
        ('increasing_stations', '六个径向站点顺序',
         all(math.isfinite(value) for value in stations)
         and all(left < right for left, right in zip(stations, stations[1:])),
         '站点由83 mm内径向坐标起逐段累加，须严格递增。'),
        ('positive_section_area', '参数公式截面积', math.isfinite(area) and area > 0,
         '沿用现有截面积公式，面积须为有限正值。'),
    ]
    for key, label, passed, message in rules:
        checks.append({'id': key, 'label': label, 'status': 'passed' if passed else 'failed',
                       'message': message})
    checks.extend([
        {'id': 'cad_curve_validity', 'label': '完整CAD轮廓及自交', 'status': 'unknown',
         'message': '参数站点检查未覆盖CAD过渡曲线，实际几何仍由原生更新与核验确认。'},
        {'id': 'manufacturing_thickness', 'label': '最小制造厚度', 'status': 'unknown',
         'message': '工程规范尚未配置最小制造厚度与过渡圆角门槛。'},
        {'id': 'constant_outer_radius', 'label': '恒定外半径要求', 'status': 'unknown',
         'message': '当前模型按五段跨度累加得到外半径，尚未配置恒定外半径约束。'},
    ])
    result['ok'] = all(check['status'] != 'failed' for check in checks)
    result['status'] = 'passed' if result['ok'] else 'failed'
    result.update(station_radii_mm=stations, station_half_heights_mm=heights,
                  minimum_station_thickness_mm=2.0 * min(half_heights),
                  outer_radius_mm=stations[-1], section_area_m2=area)
    # Keep failed numerical checks serializable even for unsupported extreme inputs.
    for key in ('station_radii_mm', 'station_half_heights_mm'):
        result[key] = [value if math.isfinite(value) else None for value in result[key]]
    for key in ('minimum_station_thickness_mm', 'outer_radius_mm', 'section_area_m2'):
        if not math.isfinite(result[key]):
            result[key] = None
    return result


def require_geometry(x, problem):
    result = inspect_geometry(x, problem)
    if not result['ok']:
        failed = [check['label'] for check in result['checks'] if check['status'] == 'failed']
        raise ControllerError('Geometry precheck failed: ' + ', '.join(failed))
    return result


def _stress(record):
    if not isinstance(record, dict):
        return None
    value = record.get('stressMPa', record.get('stress_mpa'))
    try:
        number = bridge.finite_number(value, 'diagnostic stress')
        return number if number > 0 else None
    except bridge.BridgeError:
        return None


def result_diagnostics(detail, *, native=None, baseline=None, previous=None):
    """Report observations without changing native-result acceptance or ranking."""
    if native is None:
        try:
            native = read_json(detail['nativeReportFile'])
        except (KeyError, OSError, bridge.BridgeError):
            native = {}
    actual = native.get('actual', {}) if isinstance(native, dict) else {}
    raw_quality = actual.get('mesh_quality_after', {}) if isinstance(actual, dict) else {}
    if not isinstance(raw_quality, dict):
        raw_quality = {}
    quality = {'status': 'unavailable', 'metrics': {}, 'acceptance_status': 'unknown',
               'message': '该原生记录未取得可用的网格质量统计。'}
    raw_metrics = raw_quality.get('metrics', {})
    if isinstance(raw_metrics, dict):
        for name in ('element_quality', 'jacobian_ratio'):
            metric = raw_metrics.get(name, {})
            if not isinstance(metric, dict) or metric.get('status') != 'available':
                continue
            try:
                numbers = {key: bridge.finite_number(metric[key], 'mesh ' + key)
                           for key in ('minimum', 'maximum', 'mean', 'standard_deviation')}
                if (numbers['minimum'] > numbers['mean'] or numbers['mean'] > numbers['maximum']
                        or numbers['standard_deviation'] < 0):
                    continue
            except (KeyError, bridge.BridgeError):
                continue
            quality['metrics'][name] = dict(numbers, status='available',
                                            source='Mechanical.Model.Mesh statistics')
        if quality['metrics']:
            quality.update(status='available' if len(quality['metrics']) == 2 else 'partial',
                           message='已采集原生网格质量统计，工程验收门槛尚未配置。')
        quality['selector_restored'] = raw_quality.get('selector_restored')
    checks = [{'id': 'mesh_quality_limits', 'label': '网格质量验收门槛', 'status': 'unknown',
               'message': quality['message']}]
    warnings = []
    if raw_quality.get('selector_restored') is False:
        warnings.append('网格质量统计选择器还原失败，请检查原生诊断记录。')
    changes = []
    current = _stress(detail)
    for name, reference in (('baseline', baseline), ('previous', previous)):
        stress = _stress(reference)
        if current is None or stress is None:
            continue
        delta = (current - stress) / stress * 100.0
        warning = not math.isfinite(delta) or abs(delta) >= STRESS_CHANGE_WARNING_PERCENT
        changes.append({'reference': name, 'reference_stress_mpa': stress,
                        'current_stress_mpa': current,
                        'relative_change_percent': delta if math.isfinite(delta) else None,
                        'status': 'warning' if warning else 'observed'})
        if warning:
            label = '基准' if name == 'baseline' else '上一实测'
            if math.isfinite(delta):
                warnings.append('最大应力相对%s变化%+.2f%%，达到25%%诊断提醒线，建议结合几何与网格复核。'
                                % (label, delta))
            else:
                warnings.append('最大应力相对%s的变化比值超出有限数值范围，请复核原生数值与单位。' % label)
    return {'schema_version': RESULT_POLICY, 'ok': True,
            'status': 'warning' if warnings else 'observed' if quality['metrics'] else 'unknown',
            'acceptance_effect': 'diagnostic_only', 'mesh_quality': quality, 'checks': checks,
            'stress_changes': changes, 'stress_change_warning_percent': STRESS_CHANGE_WARNING_PERCENT,
            'threshold_source': 'diagnostic_default_v1', 'warnings': warnings}
