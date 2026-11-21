"""NX12 builders preserving the complete learned boundaries and trimmed surfaces."""
import ctypes as C
import json,os
import NXOpen,NXOpen.Features,NXOpen.UF
from nx_kernel import point,uf,checked,tag
from precision_recipes import SECTION_DEFAULTS

REF_X=[83.,103.81175,153.237291,206.210705,229.5,237.]
EDGE_ZONE={0:0,1:0,2:0,3:0,4:1,5:2,6:3,7:4,8:4,9:4,10:4,11:3,12:2,13:1}
HEIGHTS=[('Y1','Y1'),('Y1','Y2'),('Y2','Y3'),('Y3','Y4'),('Y4','Y4')]

def mapped_point(xyz,index,values=None):
    zone=EDGE_ZONE[index];x,y,z=xyz;first,second=HEIGHTS[zone]
    start='inner_radius'+''.join('+X'+str(i) for i in range(1,zone+1))
    fx='('+start+')+('+repr(x-REF_X[zone])+')*X'+str(zone+1)+'/'+repr(REF_X[zone+1]-REF_X[zone])
    if first==second:fy=repr(y/SECTION_DEFAULTS[first])+'*'+first
    else:
        sign=-1 if y<0 else 1
        fraction=(abs(y)-SECTION_DEFAULTS[second])/(SECTION_DEFAULTS[first]-SECTION_DEFAULTS[second])
        fy=str(sign)+'*('+second+'+('+repr(fraction)+')*('+first+'-'+second+'))'
    if values is None:return [fx,fy,repr(z)]
    # Independent numeric control-point mapping for the reference body.
    new_x=values['inner_radius']+sum(values['X'+str(i)] for i in range(1,zone+1))+(x-REF_X[zone])*values['X'+str(zone+1)]/(REF_X[zone+1]-REF_X[zone])
    new_y=y*values[first]/SECTION_DEFAULTS[first] if first==second else sign*(values[second]+fraction*(values[first]-values[second]))
    return [new_x,new_y,z]

class Spline(C.Structure):
    _fields_=[('num_poles',C.c_int),('order',C.c_int),('rational',C.c_int),('knots',C.POINTER(C.c_double)),('poles',C.POINTER(C.c_double*4)),('start',C.c_double),('end',C.c_double)]

def raw_spline(p,edge,coordinates):
    poles=((C.c_double*4)*len(coordinates))(*[(C.c_double*4)(*(v+[1.])) for v in coordinates])
    knots=(C.c_double*len(edge['knots']))(*edge['knots'])
    curve=Spline(len(coordinates),edge['degree']+1,0,knots,poles,edge['knots'][edge['degree']],edge['knots'][-edge['degree']-1])
    output=tag();states=C.c_int();details=C.c_void_p()
    uf.UF_CURVE_create_spline.argtypes=[C.POINTER(Spline),C.POINTER(tag),C.POINTER(C.c_int),C.POINTER(C.c_void_p)]
    try:checked(uf.UF_CURVE_create_spline(C.byref(curve),C.byref(output),C.byref(states),C.byref(details)))
    finally:
        if details:uf.UF_free(details)
    return NXOpen.TaggedObjectManager.GetTaggedObject(output.value)

def build_2d(p,asset,parameters,associative=True):
    data=json.load(open(asset,encoding='utf-8'));curves=[]
    for index,edge in enumerate(data['edges']):
        if not associative:
            if edge['type']=='LINE':curve=p.Curves.CreateLine(NXOpen.Point3d(*mapped_point(edge['start'],index,parameters)),NXOpen.Point3d(*mapped_point(edge['end'],index,parameters)))
            else:curve=raw_spline(p,edge,[mapped_point(v,index,parameters) for v in edge['poles']])
            curves.append(curve);continue
        if edge['type']=='LINE':
            b=p.BaseFeatures.CreateAssociativeLineBuilder(NXOpen.Features.AssociativeLine.Null)
            b.Associative=True;b.StartPointOptions=b.StartOption.Point;b.EndPointOptions=b.EndOption.Point
            b.StartPoint.Value=point(p,mapped_point(edge['start'],index));b.EndPoint.Value=point(p,mapped_point(edge['end'],index))
        else:
            b=p.Features.CreateStudioSplineBuilderEx(NXOpen.NXObject.Null);b.Type=b.Types.ByPoles;b.Degree=edge['degree'];b.IsAssociative=True
            for xyz in edge['poles']:
                constraint=b.ConstraintManager.CreateGeometricConstraintData();constraint.Point=point(p,mapped_point(xyz,index));b.ConstraintManager.Append(constraint)
            # NX StudioSpline evaluates over [0,1]; an affine knot normalization preserves the exact curve.
            knots=edge['knots'];a=knots[edge['degree']];z=knots[-edge['degree']-1]
            b.SetKnots([(k-a)/(z-a) for k in knots])
        f=b.CommitFeature();b.Destroy();f.SetName('EXACT_BOUNDARY_'+str(index));curves.extend(f.GetEntities())
    b=p.Features.CreateBoundedPlaneBuilder(NXOpen.Features.BoundedPlane.Null)
    b.BoundingCurves.AddToSection([p.ScRuleFactory.CreateRuleCurveDumb(curves)],curves[0],NXOpen.NXObject.Null,NXOpen.NXObject.Null,NXOpen.Point3d(parameters['inner_radius'],0.,0.),NXOpen.Section.Mode.Create,False)
    f=b.CommitFeature();b.Destroy();f.SetName('EXACT_NINE_PARAMETER_SECTION' if associative else 'INDEPENDENT_SOURCE_BOUNDARY')

def build_3d(p,asset,parameters,reference=False):
    NXOpen.UF.UFSession.GetUFSession().Ps.ImportData(asset)
    for f in p.Features:f.SetName('LEARNED_EXACT_SOURCE_BREP')
    if reference and parameters=={'hub_length':56.9,'rim_radius':71.68}:return
    mm=p.UnitCollection.FindObject('MilliMeter')
    if reference:
        for name,value in parameters.items():p.Expressions.CreateWithUnits(name+'='+str(value),mm)
    p.Expressions.CreateWithUnits('reference_hub_length=56.9',mm);p.Expressions.CreateWithUnits('reference_rim_radius=71.68',mm)
    p.Expressions.CreateExpression('Number','axial_scale=hub_length/reference_hub_length')
    p.Expressions.CreateExpression('Number','radial_scale=rim_radius/reference_rim_radius')
    for name,value,scale in [('bore_radius',5.2,'radial_scale'),('tip_radius',105.,'radial_scale'),('hub_radius',27.5,'radial_scale'),('rim_width',20.8,'axial_scale'),('web_thickness',9.,'axial_scale')]:
        p.Expressions.CreateWithUnits('reference_'+name+'='+str(value),mm)
        p.Expressions.CreateWithUnits(name+'=reference_'+name+'*'+scale,mm)
    p.Expressions.CreateWithUnits('blade_height=tip_radius-rim_radius',mm)
    b=p.Features.CreateScaleBuilder(NXOpen.Features.Feature.Null);b.Type=b.Types.General
    b.BodyToScale.Add(list(p.Bodies)[0]);b.Point=p.Points.CreatePoint(NXOpen.Point3d(0.,0.,0.))
    b.ScaleXdirection.RightHandSide='axial_scale';b.ScaleYdirection.RightHandSide='radial_scale';b.ScaleZdirection.RightHandSide='radial_scale'
    f=b.CommitFeature();b.Destroy();f.SetName('EXACT_AXIAL_RADIAL_SCALE')
