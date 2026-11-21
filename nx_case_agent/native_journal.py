"""NX 12 journal: preserve complete learned geometry; reject failed precision checks."""
import os,sys,json,traceback,hashlib
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import NXOpen
from nx_kernel import measure,kernel_check,compare_geometry
from exact_geometry import build_2d,build_3d
job=json.load(open(os.environ['NX_AGENT_JOB'],encoding='utf-8'))
s=NXOpen.Session.GetSession()
root=os.path.dirname(os.path.abspath(__file__))
output=os.path.join(job['folder'],'model.prt')

def progress(stage):
    with open(os.path.join(job['folder'],'progress.json'),'w',encoding='utf-8') as stream:json.dump({'stage':stage},stream)
    print(stage,flush=True)

def update_derived(p):
    result={}
    for name,data in job.get('derived_parameters',{}).items():
        expression=p.Expressions.FindObject(name)
        result[name]=dict(data,value=expression.Value,expression=expression.RightHandSide)
    if result and abs(result['blade_height']['value']-(result['tip_radius']['value']-p.Expressions.FindObject('rim_radius').Value))>1e-8:raise RuntimeError('Derived blade height mismatch')
    return result

try:
    progress('creating_complete_geometry')
    p=s.Parts.NewDisplay(output,NXOpen.Part.Units.Millimeters)
    for name,value in job['parameters'].items():p.Expressions.CreateWithUnits(name+'='+str(value),p.UnitCollection.FindObject('MilliMeter'))
    asset=os.path.join(root,'assets','geom2d_boundary.json' if job['model_id']=='geom2d' else 'new160.x_t')
    from precision_recipes import ASSETS
    source_hash=hashlib.sha256(open(asset,'rb').read()).hexdigest()
    if source_hash!=ASSETS[os.path.basename(asset)]:raise RuntimeError('Precise geometry asset integrity mismatch')
    if job['model_id']=='geom2d':build_2d(p,asset,job['parameters'])
    else:build_3d(p,asset,job['parameters'])
    baseline=measure(p)
    expected=(False,1,14) if job['model_id']=='geom2d' else (True,44,122)
    if (baseline['solid'],baseline['faces'],baseline['edges'])!=expected:raise RuntimeError('Source topology was not preserved')
    progress('checking_kernel')
    baseline_checks=kernel_check(p);tests=[]
    metric='area_mm2' if job['model_id']=='geom2d' else 'volume_mm3'
    for name,value in job['parameters'].items():
        progress('testing_parameter_'+name)
        expression=p.Expressions.FindObject(name);original=expression.RightHandSide
        mark=s.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible,'precise parameter verification')
        delta=.05 if value+.05<=job['parameter_bounds'][name][1] else -.05
        p.Expressions.Edit(expression,str(value+delta));errors=s.UpdateManager.DoUpdate(mark)
        changed=measure(p)
        if errors:raise RuntimeError('Parameter update failed: '+name)
        if abs(changed[metric]-baseline[metric])<1e-7 and max(abs(a-b) for a,b in zip(changed['vertex_bounds'],baseline['vertex_bounds']))<1e-7:raise RuntimeError('Parameter does not drive geometry: '+name)
        changed_checks=kernel_check(p);derived=update_derived(p)
        p.Expressions.Edit(expression,original)
        if s.UpdateManager.DoUpdate(mark):raise RuntimeError('Parameter restore failed: '+name)
        restored=measure(p)
        if abs(restored[metric]-baseline[metric])>.001:raise RuntimeError('Geometry restore mismatch: '+name)
        tests.append({'parameter':name,'delta':delta,'update_errors':errors,'baseline':baseline[metric],'changed':changed[metric],'restored':True,'geometry_checks':{'kernel':changed_checks},'derived_parameters':derived})
    progress('saving_and_reopening')
    steps=[{'name':f.Name,'type':f.FeatureType} for f in p.Features]
    p.Save(NXOpen.BasePart.SaveComponents.TrueValue,NXOpen.BasePart.CloseAfterSave.FalseValue)
    p.Close(NXOpen.BasePart.CloseWholeTree.FalseValue,NXOpen.BasePart.CloseModified.CloseModified,None)
    p,status=s.Parts.OpenBaseDisplay(output);status.Dispose()
    reopened=measure(p);reopened_checks=kernel_check(p);derived=update_derived(p)
    if abs(reopened[metric]-baseline[metric])>.001:raise RuntimeError('Reopen geometry mismatch')
    progress('comparing_complete_reference_geometry')
    reference=s.Parts.NewDisplay(os.path.join(job['folder'],'reference_check.prt'),NXOpen.Part.Units.Millimeters)
    if job['model_id']=='geom2d':build_2d(reference,asset,job['parameters'],associative=False)
    else:build_3d(reference,asset,job['parameters'],reference=True)
    comparison=compare_geometry(p,reference)
    with open(os.path.join(job['folder'],'reference_comparison.json'),'w',encoding='utf-8') as stream:json.dump(comparison,stream,ensure_ascii=False,indent=2)
    display_status,load_status=s.Parts.SetDisplay(p,False,False)
    if load_status:load_status.Dispose()
    s.Parts.SetWork(p)
    reference.Close(NXOpen.BasePart.CloseWholeTree.FalseValue,NXOpen.BasePart.CloseModified.CloseModified,None)
    result={k:job[k] for k in ['model_id','parameters','inferred_parameters','supplied_parameters','notes','recipe_version','geometry_mode','parameterization_scope','full_feature_history_recovered','nominal_resolutions','reference_baseline','parameter_table']}
    result.update(ok=True,output=output,parameterized=True,reopened=True,geometry=reopened,geometry_checks={'kernel':baseline_checks},
                  reopened_geometry_checks=reopened_checks,reference_comparison=comparison,derived_parameters=derived,parameter_tests=tests,
                  modeling_steps=steps,method='native_full_knot_control_net_reconstruction' if job['model_id']=='geom2d' else 'learned_original_brep_with_native_axial_radial_scale',
                  exact_original_baseline=job['reference_baseline'],simplified=False,source_asset_sha256=source_hash)
    progress('complete')
except:
    result={'ok':False,'geometry_mode':'precise','simplified':False,'error':traceback.format_exc()}
with open(os.path.join(job['folder'],'result.json'),'w',encoding='utf-8') as stream:json.dump(result,stream,ensure_ascii=False,indent=2)
print(json.dumps(result,ensure_ascii=True))
