"""Measured Geomshanqu radial section, revolved around Z (millimetres)."""
from dataclasses import dataclass
import json
from pathlib import Path
from .schema import ModelingPlanError, _number


@dataclass(frozen=True)
class SectorPlan:
    angle: float = 15.0
    scale: float = 1.0
    name: str = 'reference_sector'

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value)-{'kind','angle','scale','name'}:
            raise ModelingPlanError('扇区只接受角度、整体比例和名称。')
        from .schema import _NAME
        name=value.get('name','reference_sector')
        if not isinstance(name,str) or not _NAME.fullmatch(name):
            raise ModelingPlanError('零件名称无效。')
        return cls(_number(value.get('angle',15),'angle',0.1,359),
                   _number(value.get('scale',1),'scale',0.1,10),name)

    def to_dict(self):
        return {'kind':'reference_sector',**self.__dict__}


def compile_sector(plan, output):
    template=Path(__file__).with_name('sector_journal.py.txt').read_text(encoding='utf-8')
    return template.replace('__PLAN_JSON__',repr(json.dumps(plan.to_dict()))).replace('__OUTPUT__',repr(str(output)))
