"""NX 12 Python 3.6 journal. Only registered numeric recipes are accepted."""
import os,json,traceback,math,ctypes
import NXOpen
import NXOpen.Features
import NXOpen.GeometricUtilities

job=json.load(open(os.environ['NX_AGENT_JOB'],encoding='utf-8'))
s=NXOpen.Session.GetSession()
output=os.path.join(job['folder'],'model.prt')
smooth_pairs=[]
uf=ctypes.CDLL(os.path.join(os.environ['UGII_BASE_DIR'],'NXBIN','libufun.dll'))
tag=ctypes.c_uint;integer=ctypes.c_int;real=ctypes.c_double
ip=ctypes.POINTER(integer);tp=ctypes.POINTER(tag);rp=ctypes.POINTER(real)
uf.UF_free.argtypes=[ctypes.c_void_p];uf.UF_free.restype=None
uf.UF_MODL_ask_body_consistency.argtypes=[tag,ip,ctypes.POINTER(ip),ctypes.POINTER(tp)]
uf.UF_MODL_ask_face_face_intersect.argtypes=[tag,ip,ctypes.POINTER(tp)]
uf.UF_MODL_ask_face_self_intersect.argtypes=[tag,ip,rp]
uf.UF_EVAL_initialize.argtypes=[tag,ctypes.POINTER(ctypes.c_void_p)]
uf.UF_EVAL_ask_limits.argtypes=[ctypes.c_void_p,rp]
uf.UF_EVAL_evaluate.argtypes=[ctypes.c_void_p,integer,real,rp,rp]
uf.UF_EVAL_free.argtypes=[ctypes.c_void_p]

def checked(code):
    if code:raise RuntimeError('NX UF geometry check failed, code '+str(code))

def endpoints(curve):
    evaluator=ctypes.c_void_p();checked(uf.UF_EVAL_initialize(curve.Tag,ctypes.byref(evaluator)))
    try:
        limits=(real*2)();checked(uf.UF_EVAL_ask_limits(evaluator,limits));result=[]
        for u in limits:
            xyz=(real*3)();derivative=(real*3)()
            checked(uf.UF_EVAL_evaluate(evaluator,1,u,xyz,derivative))
            result.append((list(xyz),list(derivative)))
        return result
    finally:checked(uf.UF_EVAL_free(evaluator))

def tangency_check():
    angles=[];gaps=[]
    for a,b in smooth_pairs:
        pair=min([(sum((x-y)**2 for x,y in zip(p[0],q[0])),p,q) for p in endpoints(a) for q in endpoints(b)],key=lambda item:item[0])
        distance,p,q=pair;u,v=p[1],q[1]
        cosine=abs(sum(x*y for x,y in zip(u,v)))/math.sqrt(sum(x*x for x in u)*sum(x*x for x in v))
        angle=math.degrees(math.acos(min(1.,cosine)))
        gaps.append(math.sqrt(distance));angles.append(angle)
    if max(gaps or [0])>.001 or max(angles or [0])>.01:raise RuntimeError('Smooth transition lost G1 tangency')
    return {'joins_checked':len(angles),'max_gap_mm':max(gaps or [0]),'max_tangent_angle_degrees':max(angles or [0]),'passed':True}

def kernel_check(p):
    body=list(p.Bodies)[0];count=integer();tokens=ip();tags=tp()
    try:
        checked(uf.UF_MODL_ask_body_consistency(body.Tag,ctypes.byref(count),ctypes.byref(tokens),ctypes.byref(tags)))
        if count.value:raise RuntimeError('Body consistency faults: '+str([tokens[i] for i in range(count.value)]))
    finally:
        if tokens:uf.UF_free(tokens)
        if tags:uf.UF_free(tags)
    tags=tp();count=integer()
    try:
        checked(uf.UF_MODL_ask_face_face_intersect(body.Tag,ctypes.byref(count),ctypes.byref(tags)))
        if count.value:raise RuntimeError('Face-face intersection detected: '+str(count.value))
    finally:
        if tags:uf.UF_free(tags)
    faces=list(body.GetFaces())
    for face in faces:
        fault=integer();xyz=(real*3)()
        checked(uf.UF_MODL_ask_face_self_intersect(face.Tag,ctypes.byref(fault),xyz))
        if fault.value:raise RuntimeError('Self-intersecting face: '+str(face.Tag)+', fault '+str(fault.value))
    return {'body_consistency':'passed','face_face_intersection':'passed','face_self_intersection':'passed','faces_checked':len(faces)}

def derived_check(p):
    if job['model_id']!='new160':return {}
    e=p.Expressions.FindObject('blade_height')
    value=p.Expressions.FindObject('tip_radius').Value-p.Expressions.FindObject('rim_radius').Value
    if abs(e.Value-value)>1e-8:raise RuntimeError('Derived blade height did not update')
    return {'blade_height':{'expression':e.RightHandSide,'value':e.Value,'unit':'mm'}}

def point(p,xyz):
    mm=p.UnitCollection.FindObject('MilliMeter');scalars=[]
    for formula in xyz:
        e=p.Expressions.CreateSystemExpressionWithUnits(str(formula),mm)
        scalars.append(p.Scalars.CreateScalarExpression(e,NXOpen.Scalar.DimensionalityType.Length,NXOpen.SmartObject.UpdateOption.WithinModeling))
    return p.Points.CreatePoint(*scalars,NXOpen.SmartObject.UpdateOption.WithinModeling)

def profile(p,vertices,arcs=None,splines=None):
    points=[point(p,v) for v in vertices];features=[];curves=[];segments=[]
    for i,a in enumerate(points):
        if splines and i in splines:
            b=p.Features.CreateStudioSplineBuilderEx(NXOpen.NXObject.Null)
            b.Type=b.Types.ByPoles;b.Degree=3;b.IsAssociative=True
            for pole in [a]+[point(p,v) for v in splines[i]]+[points[(i+1)%len(points)]]:
                data=b.ConstraintManager.CreateGeometricConstraintData();data.Point=pole
                b.ConstraintManager.Append(data)
            f=b.CommitFeature();b.Destroy();features.append(f);curves.extend(f.GetEntities());segments.append(f.GetEntities()[0])
            continue
        if arcs and i in arcs:
            b=p.BaseFeatures.CreateAssociativeArcBuilder(NXOpen.Features.AssociativeArc.Null)
            b.Type=b.Types.ThreePointArc;b.MidPointOptions=b.MidOption.Point;b.MidPoint.Value=point(p,arcs[i])
        else:b=p.BaseFeatures.CreateAssociativeLineBuilder(NXOpen.Features.AssociativeLine.Null)
        b.Associative=True;b.StartPointOptions=b.StartOption.Point;b.EndPointOptions=b.EndOption.Point
        b.StartPoint.Value=a;b.EndPoint.Value=points[(i+1)%len(points)]
        f=b.CommitFeature();b.Destroy();features.append(f);curves.extend(f.GetEntities());segments.append(f.GetEntities()[0])
    for i in (splines or {}):smooth_pairs.extend([(segments[(i-1)%len(segments)],segments[i]),(segments[i],segments[(i+1)%len(segments)])])
    section=p.Sections.CreateSection(.001,.001,.5)
    section.AddToSection([p.ScRuleFactory.CreateRuleCurveFeature(features)],curves[0],NXOpen.NXObject.Null,NXOpen.NXObject.Null,NXOpen.Point3d(0.,0.,0.),NXOpen.Section.Mode.Create,False)
    return section,curves

def section2d(p):
    r=['inner_radius']
    for i in range(1,6):r.append(r[-1]+'+X'+str(i))
    pairs=[[r[0],'-Y1'],[r[1],'-Y1'],[r[2],'-Y2'],[r[3],'-Y3'],[r[4],'-Y4'],[r[5],'-Y4'],[r[5],'Y4'],[r[4],'Y4'],[r[3],'Y3'],[r[2],'Y2'],[r[1],'Y1'],[r[0],'Y1']]
    vertices=[[x,y,'0'] for x,y in pairs];splines={}
    # Cubic Bezier controls impose the adjoining flat/web tangent exactly.
    for i,m0,m1 in [(1,'0','(Y2-Y3)/X3'),(3,'(Y2-Y3)/X3','0'),
                    (7,'0','(Y3-Y2)/X3'),(9,'(Y3-Y2)/X3','0')]:
        a,b=pairs[i],pairs[i+1]
        dx='(('+b[0]+')-('+a[0]+'))/3'
        splines[i]=[['('+a[0]+')+('+dx+')','('+a[1]+')+('+m0+')*('+dx+')','0'],
                    ['('+b[0]+')-('+dx+')','('+b[1]+')-('+m1+')*('+dx+')','0']]
    sec,curves=profile(p,vertices,splines=splines)
    b=p.Features.CreateBoundedPlaneBuilder(NXOpen.Features.BoundedPlane.Null)
    b.BoundingCurves.AddToSection([p.ScRuleFactory.CreateRuleCurveDumb(curves)],curves[0],NXOpen.NXObject.Null,NXOpen.NXObject.Null,NXOpen.Point3d(83.,0.,0.),NXOpen.Section.Mode.Create,False)
    f=b.CommitFeature();b.Destroy();f.SetName('LEARNED_NINE_PARAMETER_SECTION')

def disk_blade(p):
    mid='hub_radius+0.45*(rim_radius-hub_radius)'
    outer='hub_radius+0.75*(rim_radius-hub_radius)'
    shoulder='hub_radius+0.9*(rim_radius-hub_radius)'
    vertices=[['-hub_length/2','0','bore_radius'],['hub_length/2','0','bore_radius'],['hub_length/2','0','hub_radius'],
              ['web_thickness/2','0',mid],['web_thickness/2','0',outer],
              ['rim_width/2','0',shoulder],['rim_width/2','0','rim_radius'],['-rim_width/2','0','rim_radius'],
              ['-rim_width/2','0',shoulder],['-web_thickness/2','0',outer],
              ['-web_thickness/2','0',mid],['-hub_length/2','0','hub_radius']]
    splines={}
    for i in [2,4,8,10]:
        a,c=vertices[i],vertices[i+1];dz='(('+c[2]+')-('+a[2]+'))/3'
        splines[i]=[[a[0],'0','('+a[2]+')+('+dz+')'],[c[0],'0','('+c[2]+')-('+dz+')']]
    sec,curves=profile(p,vertices,splines=splines)
    b=p.Features.CreateRevolveBuilder(NXOpen.Features.Feature.Null);b.Section=sec
    origin=p.Points.CreatePoint(NXOpen.Point3d(0.,0.,0.))
    d=p.Directions.CreateDirection(NXOpen.Point3d(0.,0.,0.),NXOpen.Vector3d(1.,0.,0.),NXOpen.SmartObject.UpdateOption.WithinModeling)
    b.Axis=p.Axes.CreateAxis(origin,d,NXOpen.SmartObject.UpdateOption.WithinModeling)
    b.Limits.StartExtend.Value.RightHandSide='-sector_angle/2';b.Limits.EndExtend.Value.RightHandSide='sector_angle/2'
    disk=b.CommitFeature();b.Destroy();disk.SetName('LEARNED_DISK_SECTOR')
    sections=[]
    for t in [0.,.5,1.]:
        chord='((1-'+str(t)+')*root_chord+'+str(t)+'*tip_chord)'
        thick='((1-'+str(t)+')*blade_thickness+'+str(t)+'*tip_thickness)'
        angle='('+str(t)+'*blade_twist)'
        z='rim_radius-1+(blade_height+1)*'+str(t)
        verts=[]
        for x,y in [(-.5,0.),(0.,.5),(.5,0.),(0.,-.5)]:
            a=str(x)+'*'+chord;c=str(y)+'*'+thick
            verts.append(['('+a+')*cos('+angle+')-('+c+')*sin('+angle+')','('+a+')*sin('+angle+')+('+c+')*cos('+angle+')',z])
        sections.append(profile(p,verts)[0])
    b=p.Features.CreateThroughCurvesBuilder(NXOpen.Features.Feature.Null)
    b.BodyPreference=b.BodyPreferenceTypes.Solid
    for sec in sections:b.SectionsList.Append(sec)
    blade=b.CommitFeature();b.Destroy();blade.SetName('PARAMETRIC_TAPERED_TWISTED_BLADE')
    b=p.Features.CreateBooleanBuilder(NXOpen.Features.BooleanFeature.Null)
    b.Operation=NXOpen.Features.Feature.BooleanType.Unite
    b.Target=disk.GetBodies()[0];b.Tool=blade.GetBodies()[0]
    f=b.CommitFeature();b.Destroy();f.SetName('BLADE_DISK_UNION')
    # Only the four blade/cylindrical rim intersections; never the sector boundaries.
    edges=[e for e in f.GetBodies()[0].GetEdges()
           if set(face.SolidFaceType for face in e.GetFaces())==set([NXOpen.Face.FaceType.Cylindrical,NXOpen.Face.FaceType.Parametric])]
    if len(edges)!=4:raise RuntimeError('Expected four blade root edges, got '+str(len(edges)))
    collector=p.ScCollectors.CreateCollector()
    collector.ReplaceRules([p.ScRuleFactory.CreateRuleEdgeDumb(edges)],False)
    b=p.Features.CreateEdgeBlendBuilder(NXOpen.Features.Feature.Null)
    b.Tolerance=.001;b.AddChainset(collector,'root_fillet')
    f=b.CommitFeature();b.Destroy();f.SetName('PARAMETRIC_BLADE_ROOT_FILLET')

def measure(p):
    bodies=list(p.Bodies)
    if len(bodies)!=1:raise RuntimeError('Expected one connected body, got '+str(len(bodies)))
    body=bodies[0]
    mm=p.UnitCollection.FindObject('MilliMeter');area=p.UnitCollection.FindObject('SquareMilliMeter')
    if body.IsSolidBody:
        units=[area,p.UnitCollection.FindObject('CubicMilliMeter'),p.UnitCollection.FindObject('Kilogram'),mm,p.UnitCollection.FindObject('Newton')]
        m=p.MeasureManager.NewMassProperties(units,.999999,[body]);volume=m.Volume
    else:m=p.MeasureManager.NewFaceProperties(area,mm,.999999,list(body.GetFaces()));volume=None
    vertices=[v for edge in body.GetEdges() for v in edge.GetVertices()]
    coords=[[v.X,v.Y,v.Z] for v in vertices]
    bounds=[min(q[i] for q in coords) for i in range(3)]+[max(q[i] for q in coords) for i in range(3)]
    data={'solid':body.IsSolidBody,'faces':len(body.GetFaces()),'edges':len(body.GetEdges()),'area_mm2':m.Area,'volume_mm3':volume,'vertex_bounds':bounds}
    m.Dispose()
    if data['area_mm2']<=0 or (volume is not None and volume<=0):raise RuntimeError('Invalid body measure')
    return data

try:
    p=s.Parts.NewDisplay(output,NXOpen.Part.Units.Millimeters)
    for key,value in job['parameters'].items():
        unit=p.UnitCollection.FindObject('Degrees' if key in ['sector_angle','blade_twist'] else 'MilliMeter')
        p.Expressions.CreateWithUnits(key+'='+str(value),unit)
    if job['model_id']=='geom2d':section2d(p)
    else:
        p.Expressions.CreateWithUnits('blade_height=tip_radius-rim_radius',p.UnitCollection.FindObject('MilliMeter'))
        disk_blade(p)
    baseline=measure(p)
    checks={'kernel':kernel_check(p),'transitions':tangency_check()};derived=derived_check(p)
    if baseline['solid']!=(job['model_id']=='new160'):raise RuntimeError('Body dimensionality mismatch')
    metric='area_mm2' if job['model_id']=='geom2d' else 'volume_mm3'
    tests=[]
    for key in job['parameters']:
        expression=p.Expressions.FindObject(key);old=expression.RightHandSide
        mark=s.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible,'parameter verification')
        bounds=job.get('parameter_bounds',{}).get(key,[-float('inf'),float('inf')])
        delta=.05 if job['parameters'][key]+.05<=bounds[1] else -.05
        p.Expressions.Edit(expression,str(job['parameters'][key]+delta))
        errors=s.UpdateManager.DoUpdate(mark);changed=measure(p)
        changed_checks={'kernel':kernel_check(p),'transitions':tangency_check()};changed_derived=derived_check(p)
        bound_change=max(abs(a-b) for a,b in zip(changed['vertex_bounds'],baseline['vertex_bounds']))
        if errors or (abs(changed[metric]-baseline[metric])<1e-7 and bound_change<1e-7):raise RuntimeError('Parameter does not drive geometry: '+key)
        p.Expressions.Edit(expression,old)
        if s.UpdateManager.DoUpdate(mark):raise RuntimeError('Failed to restore parameter: '+key)
        restored=measure(p)
        if abs(restored[metric]-baseline[metric])>1e-5:raise RuntimeError('Baseline restore mismatch: '+key)
        derived_check(p)
        tests.append({'parameter':key,'delta':delta,'update_errors':errors,'baseline':baseline[metric],'changed':changed[metric],'vertex_bound_change_mm':bound_change,'restored':True,'geometry_checks':changed_checks,'derived_parameters':changed_derived})
    steps=[{'name':f.Name,'type':f.FeatureType} for f in p.Features if f.Name in ['LEARNED_NINE_PARAMETER_SECTION','LEARNED_DISK_SECTOR','PARAMETRIC_TAPERED_TWISTED_BLADE','BLADE_DISK_UNION','PARAMETRIC_BLADE_ROOT_FILLET']]
    p.Save(NXOpen.BasePart.SaveComponents.TrueValue,NXOpen.BasePart.CloseAfterSave.FalseValue)
    p.Close(NXOpen.BasePart.CloseWholeTree.FalseValue,NXOpen.BasePart.CloseModified.CloseModified,None)
    p,status=s.Parts.OpenBaseDisplay(output);status.Dispose();reopened=measure(p)
    reopened_checks=kernel_check(p);derived=derived_check(p)
    if abs(reopened[metric]-baseline[metric])>1e-5:raise RuntimeError('Reopen verification mismatch')
    result={'ok':True,'model_id':job['model_id'],'output':output,'parameterized':True,'reopened':True,'parameters':job['parameters'],
            'inferred_parameters':job['inferred_parameters'],'geometry':reopened,'notes':job['notes'],
            'recipe_version':job.get('recipe_version'),'derived_parameters':derived,'parameter_table':job.get('parameter_table',[]),'modeling_steps':steps,
            'geometry_checks':checks,'reopened_geometry_checks':reopened_checks,'parameter_tests':tests}
except:result={'ok':False,'error':traceback.format_exc()}
with open(os.path.join(job['folder'],'result.json'),'w',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
print(json.dumps(result,ensure_ascii=True))
