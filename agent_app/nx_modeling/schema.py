"""Validated, CAD-neutral modeling plan.

The public API deliberately accepts a small operation vocabulary.  It is a
stable boundary between an LLM/UI and vendor-specific NXOpen journals.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re


class ModelingPlanError(ValueError):
    pass


_NAME = re.compile(r'[A-Za-z0-9_\-\u4e00-\u9fff]{1,64}')


def _number(value, name, low=0.001, high=100000.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelingPlanError(name + ' 必须是数值。')
    value = float(value)
    if not low <= value <= high:
        raise ModelingPlanError(f'{name} 必须在 {low:g} 到 {high:g} mm 之间。')
    return value


@dataclass(frozen=True)
class DiskPlan:
    name: str
    outer_diameter: float
    thickness: float
    center_hole_diameter: float = 0.0
    bolt_circle_diameter: float = 0.0
    bolt_hole_diameter: float = 0.0
    bolt_hole_count: int = 0
    output_format: str = 'prt'

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict):
            raise ModelingPlanError('建模计划必须是对象。')
        allowed = {'kind', 'name', 'outer_diameter', 'thickness', 'center_hole_diameter',
                   'bolt_circle_diameter', 'bolt_hole_diameter', 'bolt_hole_count', 'output_format'}
        extra = set(value) - allowed
        if extra:
            raise ModelingPlanError('未知建模字段：' + ', '.join(sorted(extra)))
        if value.get('kind', 'disk') != 'disk':
            raise ModelingPlanError('第一版仅支持已注册的 disk 参数化模板。')
        name = value.get('name', 'disk_model')
        if not isinstance(name, str) or _NAME.fullmatch(name) is None:
            raise ModelingPlanError('名称只能包含中英文、数字、下划线或连字符，最长64字符。')
        outer = _number(value.get('outer_diameter'), 'outer_diameter')
        thickness = _number(value.get('thickness'), 'thickness')
        center = _number(value.get('center_hole_diameter', 0), 'center_hole_diameter', 0, outer)
        bcd = _number(value.get('bolt_circle_diameter', 0), 'bolt_circle_diameter', 0, outer)
        bhd = _number(value.get('bolt_hole_diameter', 0), 'bolt_hole_diameter', 0, outer)
        count = value.get('bolt_hole_count', 0)
        if type(count) is not int or not 0 <= count <= 128:
            raise ModelingPlanError('bolt_hole_count 必须是0到128的整数。')
        if center >= outer:
            raise ModelingPlanError('中心孔必须小于外径。')
        if count:
            if not bcd or not bhd:
                raise ModelingPlanError('孔阵列需要节圆直径和孔径。')
            if bcd / 2 + bhd / 2 >= outer / 2:
                raise ModelingPlanError('孔阵列超出圆盘外径。')
            if bcd / 2 - bhd / 2 <= center / 2:
                raise ModelingPlanError('孔阵列与中心孔相交。')
            if count > 1 and bcd * math.sin(math.pi / count) <= bhd:
                raise ModelingPlanError('相邻螺栓孔相交或相切。')
        elif bcd or bhd:
            raise ModelingPlanError('孔数为0时不能设置孔阵列尺寸。')
        fmt = value.get('output_format', 'prt')
        if fmt != 'prt':
            raise ModelingPlanError('第一版 output_format 仅支持 prt。')
        return cls(name, outer, thickness, center, bcd, bhd, count, fmt)

    def to_dict(self):
        return {'kind': 'disk', **self.__dict__}
