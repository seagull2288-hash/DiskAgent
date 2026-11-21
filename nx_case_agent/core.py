"""Fail-closed planning and execution for two precise learned NX cases."""
import hashlib,json,math,os,re,subprocess,threading,unicodedata,uuid
from pathlib import Path
from .precision_recipes import RECIPES,VERSION,ASSETS,SECTION_NOMINAL,THREE_D_DERIVED
ROOT=Path(__file__).resolve().parent
LOCK=threading.Lock()

def models():return json.loads(json.dumps(RECIPES,ensure_ascii=False))

def validate_assets():
    for name,expected in ASSETS.items():
        path=ROOT/'assets'/name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            raise ValueError('精确几何资产缺失或校验失败：'+name+'。请复制完整目录，不能退回简化建模。')

def finite(name,value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):raise ValueError('参数必须是有限数值：'+name)

def plan(description, parameters=None):
    if not isinstance(description,str) or not description.strip() or len(description)>20000:raise ValueError('请输入一种案例的建模描述，最长20000字符。')
    text=unicodedata.normalize('NFKC',description).replace('**','').replace('__','').replace('`','')
    t=text.lower();is2=bool(re.search(r'geom2d|二维|2d|九参数',t));is3=bool(re.search(r'new160|新160|单叶片|带.*叶片|三维.*轮盘|3d',t))
    if is2 and is3 and not re.search(r'geom2d.*(?:和|与).*new160|二维轮盘.*(?:和|与).*三维',t):is2=not bool(re.search(r'三维|new160|新160|单叶片',t))
    if is2==is3:raise ValueError('请描述一种模型：二维轮盘截面，或单叶片三维轮盘局部。')
    key='geom2d' if is2 else 'new160';recipe=RECIPES[key];values=dict(recipe['parameters']);supplied={};resolutions=[]
    number=r'([-+]?(?:\d+(?:\.\d*)?|\.\d+))'
    for name,aliases in recipe['aliases'].items():
        names=sorted(set([name]+aliases),key=len,reverse=True)
        pattern=r'(?<![a-z0-9_])(?:'+'|'.join(re.escape(n) for n in names)+r')\s*(?:(?:为|是|取|初始取|约|大约|=|:|：)\s*)*'+number
        found=re.findall(pattern,text,re.I)
        if found:
            if len(set(map(float,found)))>1:raise ValueError('同一参数给出了不同数值：'+name)
            supplied[name]=float(found[-1])
    for token in re.findall(r'\b([XY]\d+)\s*[=:：]',text,re.I):
        if token.upper() not in values:raise ValueError('未知参数：'+token)
    if key=='new160':
        angles=re.findall(number+r'\s*[°度]\s*的?\s*(?:局部)?扇区',text)
        if angles:
            if len(set(map(float,angles)))>1 or ('sector_angle' in supplied and not math.isclose(supplied['sector_angle'],float(angles[-1]))):raise ValueError('同一参数给出了不同数值：sector_angle')
            supplied['sector_angle']=float(angles[-1])
    overrides=parameters if parameters is not None else {}
    if not isinstance(overrides,dict) or set(overrides)-set(values):raise ValueError('当前精确模式可独立编辑：'+', '.join(values)+'；其余三维尺寸未恢复独立驱动，不能退回简化模型。')
    for name,value in supplied.items():finite(name,value)
    nominal=SECTION_NOMINAL if key=='geom2d' else {'hub_length':56.9,'rim_radius':71.7}
    for name,value in supplied.items():
        if name in values:
            if name not in overrides and math.isclose(value,nominal.get(name,values[name]),abs_tol=1e-8) and value!=values[name]:
                resolutions.append({'parameter':name,'requested_nominal':value,'reference_exact':values[name],'reason':'reference_rounded_dimension'})
            else:values[name]=value
    for name,value in overrides.items():finite(name,value);values[name]=value
    for name,value in values.items():
        low,high=recipe['bounds'][name]
        if not low<=value<=high:raise ValueError(name+'超出精确配方允许范围。')
    derived={}
    if key=='geom2d':
        if not values['Y1']>values['Y2'] or not values['Y4']>values['Y3']:raise ValueError('内外端半厚度须大于相邻薄盘半厚度。')
    else:
        radial=values['rim_radius']/71.68;axial=values['hub_length']/56.9
        for name,(formula,base,label) in THREE_D_DERIVED.items():
            value=base*(axial if name in ['rim_width','web_thickness'] else radial)
            derived[name]={'expression':formula,'value':value,'unit':'mm','label':label}
        for name,value in supplied.items():
            if name in values:continue
            expected=7.66 if name=='sector_angle' else derived.get(name,{}).get('value')
            if expected is None:raise ValueError('精确模式尚不能独立设置 '+name+'；保留原始曲面，不能替换为猜测截面。')
            if not math.isclose(value,expected,abs_tol=.005):
                driver='hub_length' if name in ['rim_width','web_thickness'] else 'rim_radius'
                if name!='sector_angle' and driver in overrides:
                    resolutions.append({'parameter':name,'requested_nominal':value,'actual_coupled':expected,'reason':'explicit_independent_parameter_override'})
                else:raise ValueError(name+'不是独立参数，与精确参考及当前缩放关系不符。')
    table=[{'name':n,'label':recipe['aliases'][n][0],'value':v,'unit':'mm','source':'description_or_override' if n in supplied or n in overrides else 'reference_geometry','editable':True} for n,v in values.items()]
    table.extend({'name':n,'label':d['label'],'value':d['value'],'unit':'mm','source':'derived','expression':d['expression'],'editable':False} for n,d in derived.items())
    baseline=all(math.isclose(v,recipe['parameters'][n],abs_tol=1e-10) for n,v in values.items())
    return {'ok':True,'kind':'learned_case','model_id':key,'description':description,'parameters':values,
        'supplied_parameters':sorted(set(supplied)|set(overrides)),'inferred_parameters':{n:v for n,v in values.items() if n not in supplied and n not in overrides},
        'parameterized':True,'parameterization_scope':'nine_parameter_exact_control_net' if is2 else 'axial_and_radial_scale_only',
        'full_feature_history_recovered':False,'geometry_mode':'precise','recipe_version':VERSION,'derived_parameters':derived,
        'parameter_table':table,'parameter_bounds':recipe['bounds'],'nominal_resolutions':resolutions,'reference_baseline':baseline,
        'learned':recipe['learned'],'notes':recipe['notes'],'summary_zh':'已匹配'+recipe['title']+'，保留完整参考几何。'}

def execute(description, parameters=None, nx_home=None, jobs_root=None):
    p=plan(description,parameters);validate_assets()
    home=Path(nx_home or os.environ.get('NX_AGENT_NXBIN') or (Path(os.environ['UGII_BASE_DIR'])/'NXBIN' if os.environ.get('UGII_BASE_DIR') else r'D:\UG12.0\UG\NXBIN'))
    if not (home/'run_journal.exe').is_file():raise ValueError('请设置NX_AGENT_NXBIN为NX12的NXBIN目录。')
    with LOCK:
        folder=Path(jobs_root or os.environ.get('NX_AGENT_JOBS') or ROOT/'jobs')/uuid.uuid4().hex;folder.mkdir(parents=True)
        job=dict(p,folder=str(folder.resolve()));(folder/'job.json').write_text(json.dumps(job,ensure_ascii=False,indent=2),encoding='utf-8')
        env=os.environ.copy();env['NX_AGENT_JOB']=str((folder/'job.json').resolve());env['UGII_BASE_DIR']=str(home.parent);env['UGII_ROOT_DIR']=str(home.parent/'UGII')
        env['PATH']=str(home)+os.pathsep+env['UGII_ROOT_DIR']+os.pathsep+env.get('PATH','')
        try:proc=subprocess.run([str(home/'run_journal.exe'),str(ROOT/'native_journal.py')],cwd=str(home.parent/'UGII'),env=env,stdin=subprocess.DEVNULL,capture_output=True,timeout=600)
        except subprocess.TimeoutExpired as exc:
            (folder/'stdout.log').write_bytes(exc.stdout or b'');(folder/'stderr.log').write_bytes(exc.stderr or b'')
            raise RuntimeError('NX精确几何检查超时，检查任务：'+str(folder)) from exc
        (folder/'stdout.log').write_bytes(proc.stdout);(folder/'stderr.log').write_bytes(proc.stderr)
        result_file=folder/'result.json'
        if not result_file.is_file():raise RuntimeError('NX未返回核验结果，检查：'+str(folder))
        result=json.loads(result_file.read_text(encoding='utf-8'))
        if proc.returncode:result.update(ok=False,process_returncode=proc.returncode)
        result['job_directory']=str(folder.resolve());return result
