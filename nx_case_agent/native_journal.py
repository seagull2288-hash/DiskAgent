"""NX 12 Python 3.6 journal. Only registered numeric recipes are accepted."""
import os,json,traceback,math
import NXOpen
import NXOpen.Features
import NXOpen.GeometricUtilities

job=json.load(open(os.environ['NX_AGENT_JOB'],encoding='utf-8'))
s=NXOpen.Session.GetSession()
output=os.path.join(job['folder'],'model.prt')

def point(p,xyz):
    mm=p.UnitCollection.FindObject('MilliMeter');scalars=[]
    for formula in xyz:
        e=p.Expressions.CreateSystemExpressionWithUnits(str(formula),mm)
        scalars.append(p.Scalars.CreateScalarExpression(e,NXOpen.Scalar.DimensionalityType.Length,NXOpen.SmartObject.UpdateOption.WithinModeling))
    return p.Points.CreatePoint(*scalars,NXOpen.SmartObject.UpdateOption.WithinModeling)

def profile(p,vertices,arcs=None):
    points=[point(p,v) for v in vertices];features=[];curves=[]
    for i,a in enumerate(points):
        if arcs and i in arcs:
            b=p.BaseFeatures.CreateAssociativeArcBuilder(NXOpen.Features.AssociativeArc.Null)
            b.Type=b.Types.ThreePointArc;b.MidPointOptions=b.MidOption.Point;b.MidPoint.Value=point(p,arcs[i])
        else:b=p.BaseFeatures.CreateAssociativeLineBuilder(NXOpen.Features.AssociativeLine.Null)
        b.Associative=True;b.StartPointOptions=b.StartOption.Point;b.EndPointOptions=b.EndOption.Point
        b.StartPoint.Value=a;b.EndPoint.Value=points[(i+1)%len(points)]
        f=b.CommitFeature();b.Destroy();features.append(f);curves.extend(f.GetEntities())
    section=p.Sections.CreateSection(.001,.001,.5)
    section.AddToSection([p.ScRuleFactory.CreateRuleCurveFeature(features)],curves[0],NXOpen.NXObject.Null,NXOpen.NXObject.Null,NXOpen.Point3d(0.,0.,0.),NXOpen.Section.Mode.Create,False)
    return section,curves

def section2d(p):
    r=['inner_radius']
    for i in range(1,6):r.append(r[-1]+'+X'+str(i))
    pairs=[[r[0],'-Y1'],[r[1],'-Y1'],[r[2],'-Y2'],[r[3],'-Y3'],[r[4],'-Y4'],[r[5],'-Y4'],[r[5],'Y4'],[r[4],'Y4'],[r[3],'Y3'],[r[2],'Y2'],[r[1],'Y1'],[r[0],'Y1']]
    vertices=[[x,y,'0'] for x,y in pairs];arcs={}
    for i,k in [(1,.1650035509236539),(3,.1305116584994447),(7,.1305116584994447),(9,.1650035509236539)]:
        a,b=pairs[i],pairs[i+1]
        arcs[i]=['(('+a[0]+')+('+b[0]+'))/2-('+str(k)+')*(('+b[1]+')-('+a[1]+'))',
                 '(('+a[1]+')+('+b[1]+'))/2+('+str(k)+')*(('+b[0]+')-('+a[0]+'))','0']
    sec,curves=profile(p,vertices,arcs)
    b=p.Features.CreateBoundedPlaneBuilder(NXOpen.Features.BoundedPlane.Null)
    b.BoundingCurves.AddToSection([p.ScRuleFactory.CreateRuleCurveDumb(curves)],curves[0],NXOpen.NXObject.Null,NXOpen.NXObject.Null,NXOpen.Point3d(83.,0.,0.),NXOpen.Section.Mode.Create,False)
    f=b.CommitFeature();b.Destroy();f.SetName('LEARNED_NINE_PARAMETER_SECTION')

def disk_blade(p):
    vertices=[['-hub_length/2','0','bore_radius'],['hub_length/2','0','bore_radius'],['hub_length/2','0','hub_radius'],
              ['web_thickness/2','0','(hub_radius+rim_radius)/2'],['web_thickness/2','0','rim_radius-4'],
              ['rim_width/2','0','rim_radius-4'],['rim_width/2','0','rim_radius'],['-rim_width/2','0','rim_radius'],
              ['-rim_width/2','0','rim_radius-4'],['-web_thickness/2','0','rim_radius-4'],
              ['-web_thickness/2','0','(hub_radius+rim_radius)/2'],['-hub_length/2','0','hub_radius']]
    sec,curves=profile(p,vertices)
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
        z='(rim_radius-1)*(1-'+str(t)+')+tip_radius*'+str(t)
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
    else:disk_blade(p)
    baseline=measure(p)
    if baseline['solid']!=(job['model_id']=='new160'):raise RuntimeError('Body dimensionality mismatch')
    metric='area_mm2' if job['model_id']=='geom2d' else 'volume_mm3'
    tests=[]
    for key in job['parameters']:
        expression=p.Expressions.FindObject(key);old=expression.RightHandSide
        mark=s.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible,'parameter verification')
        p.Expressions.Edit(expression,str(job['parameters'][key]+.05))
        errors=s.UpdateManager.DoUpdate(mark);changed=measure(p)
        bound_change=max(abs(a-b) for a,b in zip(changed['vertex_bounds'],baseline['vertex_bounds']))
        if errors or (abs(changed[metric]-baseline[metric])<1e-7 and bound_change<1e-7):raise RuntimeError('Parameter does not drive geometry: '+key)
        p.Expressions.Edit(expression,old)
        if s.UpdateManager.DoUpdate(mark):raise RuntimeError('Failed to restore parameter: '+key)
        restored=measure(p)
        if abs(restored[metric]-baseline[metric])>1e-5:raise RuntimeError('Baseline restore mismatch: '+key)
        tests.append({'parameter':key,'delta':.05,'update_errors':errors,'baseline':baseline[metric],'changed':changed[metric],'vertex_bound_change_mm':bound_change,'restored':True})
    p.Save(NXOpen.BasePart.SaveComponents.TrueValue,NXOpen.BasePart.CloseAfterSave.FalseValue)
    p.Close(NXOpen.BasePart.CloseWholeTree.FalseValue,NXOpen.BasePart.CloseModified.CloseModified,None)
    p,status=s.Parts.OpenBaseDisplay(output);status.Dispose();reopened=measure(p)
    if abs(reopened[metric]-baseline[metric])>1e-5:raise RuntimeError('Reopen verification mismatch')
    result={'ok':True,'model_id':job['model_id'],'output':output,'parameterized':True,'reopened':True,'parameters':job['parameters'],
            'inferred_parameters':job['inferred_parameters'],'geometry':reopened,'notes':job['notes'],
            'parameter_tests':tests}
except:result={'ok':False,'error':traceback.format_exc()}
with open(os.path.join(job['folder'],'result.json'),'w',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
print(json.dumps(result,ensure_ascii=True))
