"""CAD-neutral parameter expressions, planar profiles and solid features."""
from dataclasses import dataclass
import ast
import json
import math
from pathlib import Path
import re
from .schema import ModelingPlanError, _NAME, _number


def expression(text, parameters):
    if not isinstance(text, str) or len(text)>500:
        raise ModelingPlanError('坐标表达式过长或不是文本。')
    try:
        tree=ast.parse(text,mode='eval')
        def visit(n):
            if isinstance(n,ast.Expression): return visit(n.body)
            if isinstance(n,ast.Constant) and type(n.value) in (int,float): return n.value
            if isinstance(n,ast.Name) and n.id in parameters: return parameters[n.id]
            if isinstance(n,ast.UnaryOp) and isinstance(n.op,(ast.UAdd,ast.USub)):
                return visit(n.operand)*(-1 if isinstance(n.op,ast.USub) else 1)
            if isinstance(n,ast.BinOp) and isinstance(n.op,(ast.Add,ast.Sub,ast.Mult,ast.Div)):
                a,b=visit(n.left),visit(n.right)
                if isinstance(n.op,ast.Add): return a+b
                if isinstance(n.op,ast.Sub): return a-b
                if isinstance(n.op,ast.Mult): return a*b
                return a/b
            raise ValueError()
        value=float(visit(tree))
        if not math.isfinite(value) or abs(value)>100000: raise ValueError()
        return value
    except (ValueError,SyntaxError,ZeroDivisionError,RecursionError,OverflowError):
        raise ModelingPlanError('表达式只支持已定义尺寸、数字和加减乘除。') from None


@dataclass(frozen=True)
class FeaturePlan:
    name: str
    parameters: dict
    vertices: list
    arc_midpoints: dict
    operation: str
    amount: str

    @classmethod
    def parse(cls,value):
        allowed={'kind','name','parameters','vertices','arc_midpoints','operation','amount'}
        if not isinstance(value,dict) or set(value)-allowed or value.get('kind')!='feature_model':
            raise ModelingPlanError('特征计划字段无效。')
        name=value.get('name','parametric_model')
        if not isinstance(name,str) or not _NAME.fullmatch(name): raise ModelingPlanError('零件名称无效。')
        params=value.get('parameters')
        if not isinstance(params,dict) or not 1<=len(params)<=32:
            raise ModelingPlanError('需要1至32个命名参数。')
        normalized={}
        for k,v in params.items():
            if not isinstance(k,str) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]{0,39}',k) or re.fullmatch(r'p\d+',k):
                raise ModelingPlanError('参数名称须为英文且不能使用NX自动编号。')
            normalized[k]=_number(v,k,-100000,100000)
        operation=value.get('operation')
        if operation not in ('revolve','extrude'): raise ModelingPlanError('目前支持轮廓回转或拉伸。')
        vertices=value.get('vertices'); arcs=value.get('arc_midpoints',{})
        if not isinstance(vertices,list) or not 3<=len(vertices)<=64 or not isinstance(arcs,dict):
            raise ModelingPlanError('闭合轮廓需要3至64个顶点。')
        def coords(pair):
            if not isinstance(pair,list) or len(pair)!=2: raise ModelingPlanError('轮廓点须为两个坐标表达式。')
            return [expression(x,normalized) for x in pair]
        points=[coords(v) for v in vertices]
        if operation=='revolve' and (value.get('amount')!='sweep_angle' or 'sweep_angle' not in normalized):
            raise ModelingPlanError('回转角须使用命名参数 sweep_angle。')
        if any('sweep_angle' in formula for pair in vertices+list(arcs.values()) if isinstance(pair,list) for formula in pair if isinstance(formula,str)):
            raise ModelingPlanError('角度不能作为轮廓的长度坐标。')
        for i,a in enumerate(points):
            b=points[(i+1)%len(points)]
            if math.dist(a,b)<1e-5: raise ModelingPlanError('轮廓包含零长度边。')
            if operation=='revolve' and a[0]<=0: raise ModelingPlanError('回转轮廓半径必须为正。')
        for k,v in arcs.items():
            if not isinstance(k,str) or not k.isdigit() or not 0<=int(k)<len(points): raise ModelingPlanError('圆弧边编号无效。')
            a,b=points[int(k)],points[(int(k)+1)%len(points)]; m=coords(v)
            if abs((m[0]-a[0])*(b[1]-a[1])-(m[1]-a[1])*(b[0]-a[0]))<1e-5:
                raise ModelingPlanError('圆弧三点共线。')
        amount=value.get('amount')
        result=expression(amount,normalized)
        if result<=0 or operation=='revolve' and result>360: raise ModelingPlanError('特征角度或长度无效。')
        return cls(name,normalized,vertices,arcs,operation,amount)

    def to_dict(self): return {'kind':'feature_model',**self.__dict__}


def compile_features(plan,output):
    return Path(__file__).with_name('features_journal.py.txt').read_text(encoding='utf-8').replace('__PLAN_JSON__',repr(json.dumps(plan.to_dict()))).replace('__OUTPUT__',repr(str(output)))
