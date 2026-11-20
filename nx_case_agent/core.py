"""Independent planning and execution service for two learned NX recipes."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import uuid
from .recipes import RECIPES

ROOT=Path(__file__).resolve().parent
LOCK=threading.Lock()

def models():return json.loads(json.dumps(RECIPES,ensure_ascii=False))

def plan(description, parameters=None):
    if not isinstance(description,str) or not description.strip() or len(description)>20000:
        raise ValueError('请输入两种案例之一的建模描述，最长20000字符。')
    t=description.lower()
    is2=bool(re.search(r'geom2d|二维|2d|九参数',t))
    is3=bool(re.search(r'new160|新160|单叶片|带.*叶片|三维.*轮盘|3d',t))
    # A three-dimensional task may mention a 2D sketch as its construction step.
    if is2 and is3 and not re.search(r'geom2d.*(?:和|与).*new160|二维轮盘.*(?:和|与).*三维',t):
        is2=not bool(re.search(r'三维|new160|新160|单叶片',t))
    if is2==is3:raise ValueError('请描述一种模型：九参数二维轮盘截面，或带单叶片的三维轮盘局部。')
    key='geom2d' if is2 else 'new160';recipe=RECIPES[key]
    values=dict(recipe['parameters']);supplied={}
    number=r'([-+]?(?:\d+(?:\.\d*)?|\.\d+))'
    for name,aliases in recipe['aliases'].items():
        names=[name]+aliases
        pattern=r'(?<![a-z0-9_])(?:'+ '|'.join(re.escape(n) for n in sorted(set(names),key=len,reverse=True))+r')\s*(?:(?:为|是|取|初始取|约|大约|=|＝|:|：)\s*)*'+number
        found=re.findall(pattern,description,re.I)
        if found:
            if len({float(v) for v in found})>1:raise ValueError('同一参数给出了不同数值：'+name)
            supplied[name]=float(found[-1])
    # Explicit numbered X/Y settings must not be silently ignored.
    for token in re.findall(r'\b([XY]\d+)\s*[=＝:：]',description,re.I):
        if token.upper() not in values:raise ValueError('未知参数：'+token)
    if key=='new160':
        angle=re.findall(r'(?:约\s*)?'+number+r'\s*[°度]\s*的?\s*(?:局部)?扇区',description)
        if angle:
            if len(set(map(float,angle)))>1 or ('sector_angle' in supplied and not math.isclose(supplied['sector_angle'],float(angle[-1]))):raise ValueError('同一参数给出了不同数值：sector_angle')
            supplied['sector_angle']=float(angle[-1])
    if parameters is not None:
        allowed=set(values)|set(recipe.get('derived_parameters',{}))
        if not isinstance(parameters,dict) or set(parameters)-allowed:raise ValueError('参数对象含有未知尺寸。')
        supplied.update(parameters)
        # A UI/optimizer edit to the independent tip radius replaces a textual height.
        if 'tip_radius' in parameters and 'blade_height' not in parameters:supplied.pop('blade_height',None)
    for name,value in supplied.items():
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):raise ValueError('参数必须是有限数值：'+name)
        low,high=([1,600] if name=='blade_height' else recipe['bounds'][name])
        if not low<=value<=high:raise ValueError(f'{name}须在{low}至{high}之间。')
    if key=='new160' and 'blade_height' in supplied:
        tip=supplied.get('rim_radius',values['rim_radius'])+supplied['blade_height']
        if 'tip_radius' in supplied and not math.isclose(supplied['tip_radius'],tip,abs_tol=1e-6):raise ValueError('叶片高度与叶尖半径、盘体外缘半径冲突。')
        supplied['tip_radius']=tip
    values.update({n:v for n,v in supplied.items() if n in values})
    if key=='geom2d':
        if not values['Y1']>values['Y2'] or not values['Y4']>values['Y3']:raise ValueError('内外端半厚度须大于相邻薄盘半厚度。')
    else:
        p=values
        if not p['bore_radius']<p['hub_radius']<p['rim_radius']-5<p['tip_radius']-8:raise ValueError('请保持内孔、轮毂、盘缘、叶尖的半径顺序和足够过渡间距。')
        if not p['web_thickness']<p['rim_width']<p['hub_length']:raise ValueError('应满足薄盘厚度 < 外缘宽度 < 轮毂长度。')
        if p['root_chord']>p['rim_width']:raise ValueError('当前叶根弦长不能大于外缘轴向宽度。')
        if p['blade_thickness']>2*(p['rim_radius']-1)*math.sin(math.radians(p['sector_angle']/2))*.9:raise ValueError('叶根厚度超出扇区宽度。')
        if p['root_fillet']>p['blade_thickness']*.4:raise ValueError('叶根圆角半径不得超过叶根厚度的40%。')
        if not recipe['bounds']['tip_radius'][0]<=p['tip_radius']<=recipe['bounds']['tip_radius'][1]:raise ValueError('由叶片高度计算的叶尖半径超出范围。')
    warnings=list(recipe['simplifications'])
    derived=({'blade_height':{'expression':'tip_radius-rim_radius','value':values['tip_radius']-values['rim_radius'],'unit':'mm'}} if key=='new160' else {})
    table=[{'name':n,'label':recipe['aliases'][n][0],'value':v,'unit':'deg' if n in ['sector_angle','blade_twist'] else 'mm','source':'description_or_override' if n in supplied else 'design_assumption'} for n,v in values.items()]
    table.extend({'name':n,'label':recipe['aliases'][n][0],'value':d['value'],'unit':d['unit'],'source':'derived','expression':d['expression']} for n,d in derived.items())
    return {'ok':True,'kind':'learned_case','model_id':key,'description':description,'parameters':values,
            'supplied_parameters':sorted(supplied),'inferred_parameters':{n:v for n,v in values.items() if n not in supplied},
            'parameterized':True,'recipe_version':'2.1','derived_parameters':derived,'parameter_table':table,'parameter_bounds':recipe['bounds'],'learned':recipe['learned'],'notes':warnings,
            'summary_zh':'已匹配'+recipe['title']+'，缺少尺寸已由案例配方补全。'}

def execute(description, parameters=None, nx_home=None, jobs_root=None):
    p=plan(description,parameters)
    home=Path(nx_home or os.environ.get('NX_AGENT_NXBIN') or (Path(os.environ['UGII_BASE_DIR'])/'NXBIN' if os.environ.get('UGII_BASE_DIR') else r'D:\UG12.0\UG\NXBIN'))
    if not (home/'run_journal.exe').is_file():raise ValueError('请设置NX_AGENT_NXBIN为NX 12.0的NXBIN目录。')
    with LOCK:
        folder=Path(jobs_root or os.environ.get('NX_AGENT_JOBS') or ROOT/'jobs')/uuid.uuid4().hex
        folder.mkdir(parents=True)
        job=dict(p,folder=str(folder.resolve()))
        (folder/'job.json').write_text(json.dumps(job,ensure_ascii=False,indent=2),encoding='utf-8')
        env=os.environ.copy();env['NX_AGENT_JOB']=str((folder/'job.json').resolve())
        env['UGII_BASE_DIR']=str(home.parent);env['UGII_ROOT_DIR']=str(home.parent/'UGII')
        env['PATH']=str(home)+os.pathsep+env['UGII_ROOT_DIR']+os.pathsep+env.get('PATH','')
        try:
            proc=subprocess.run([str(home/'run_journal.exe'),str(ROOT/'native_journal.py')],cwd=str(home.parent/'UGII'),env=env,stdin=subprocess.DEVNULL,capture_output=True,timeout=300)
        except subprocess.TimeoutExpired as exc:
            (folder/'stdout.log').write_bytes(exc.stdout or b'');(folder/'stderr.log').write_bytes(exc.stderr or b'')
            raise RuntimeError('NX运行超时，请先检查任务目录：'+str(folder)) from exc
        (folder/'stdout.log').write_bytes(proc.stdout);(folder/'stderr.log').write_bytes(proc.stderr)
        result_file=folder/'result.json'
        if not result_file.is_file():raise RuntimeError('NX未返回核验结果，检查：'+str(folder))
        result=json.loads(result_file.read_text(encoding='utf-8'))
        if proc.returncode:result.update(ok=False,process_returncode=proc.returncode)
        result['job_directory']=str(folder.resolve())
        return result
