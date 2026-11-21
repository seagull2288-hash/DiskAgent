"""NX 12 Python 3.6 journal. Only registered numeric recipes are accepted."""
import os,json,traceback,math,ctypes
import NXOpen
import NXOpen.Features
import NXOpen.GeometricUtilities

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

def curve_samples(curve,count=9):
    evaluator=ctypes.c_void_p();checked(uf.UF_EVAL_initialize(curve.Tag,ctypes.byref(evaluator)))
    try:
        limits=(real*2)();checked(uf.UF_EVAL_ask_limits(evaluator,limits));result=[]
        for i in range(count):
            u=limits[0]+(limits[1]-limits[0])*i/(count-1)
            xyz=(real*3)();derivative=(real*3)()
            checked(uf.UF_EVAL_evaluate(evaluator,1,u,xyz,derivative))
            result.append((list(xyz),list(derivative)))
        return result
    finally:checked(uf.UF_EVAL_free(evaluator))

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

def point(p,xyz):
    mm=p.UnitCollection.FindObject('MilliMeter');scalars=[]
    for formula in xyz:
        e=p.Expressions.CreateSystemExpressionWithUnits(str(formula),mm)
        scalars.append(p.Scalars.CreateScalarExpression(e,NXOpen.Scalar.DimensionalityType.Length,NXOpen.SmartObject.UpdateOption.WithinModeling))
    return p.Points.CreatePoint(*scalars,NXOpen.SmartObject.UpdateOption.WithinModeling)

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

uf.UF_MODL_ask_face_uv_minmax.argtypes=[tag,rp]
uf.UF_MODL_ask_uv_points_containment.argtypes=[integer,rp,rp,tag,ip]
uf.UF_MODL_ask_face_props.argtypes=[tag,rp,rp,rp,rp,rp,rp,rp,rp]
uf.UF_MODL_ask_minimum_dist_3.argtypes=[integer,tag,tag,integer,rp,integer,rp,rp,rp,rp,rp]

def surface_samples(face):
    limits=(real*4)();checked(uf.UF_MODL_ask_face_uv_minmax(face.Tag,limits))
    samples=[]
    for n in [5,11,23]:
        for i in range(n):
            for j in range(n):
                u=real(limits[0]+(limits[1]-limits[0])*(i+.5)/n)
                v=real(limits[2]+(limits[3]-limits[2])*(j+.5)/n);status=integer()
                checked(uf.UF_MODL_ask_uv_points_containment(1,ctypes.byref(u),ctypes.byref(v),face.Tag,ctypes.byref(status)))
                if status.value not in [1,3]:continue
                uv=(real*2)(u.value,v.value);arrays=[(real*3)() for _ in range(6)];radii=(real*2)()
                checked(uf.UF_MODL_ask_face_props(face.Tag,uv,*(arrays+[radii])))
                samples.append(list(arrays[0]))
        if samples:return samples
    raise RuntimeError('No valid interior sample on trimmed face '+str(face.Tag))

def directed_deviation(source,target):
    points=[xyz for edge in source.GetEdges() for xyz,derivative in curve_samples(edge)]
    surfaces=[surface_samples(face) for face in source.GetFaces()]
    for samples in surfaces:points.extend(samples)
    max_distance=0.;max_accuracy=0.
    for xyz in points:
        guess=(real*3)(*xyz);zero=(real*3)();p1=(real*3)();p2=(real*3)();distance=real();accuracy=real()
        checked(uf.UF_MODL_ask_minimum_dist_3(2,0,target.Tag,1,guess,0,zero,ctypes.byref(distance),p1,p2,ctypes.byref(accuracy)))
        max_distance=max(max_distance,distance.value);max_accuracy=max(max_accuracy,accuracy.value)
    return {'samples':len(points),'faces_sampled':len(surfaces),'max_distance_mm':max_distance,'max_solver_accuracy_mm':max_accuracy}

def compare_geometry(candidate,reference,tolerance=.001):
    a=measure(candidate);b=measure(reference)
    forward=directed_deviation(list(candidate.Bodies)[0],list(reference.Bodies)[0])
    reverse=directed_deviation(list(reference.Bodies)[0],list(candidate.Bodies)[0])
    area_error=abs(a['area_mm2']-b['area_mm2']);volume_error=abs((a['volume_mm3'] or 0)-(b['volume_mm3'] or 0))
    topology=a['solid']==b['solid'] and a['faces']==b['faces'] and a['edges']==b['edges']
    passed=topology and max(forward['max_distance_mm'],reverse['max_distance_mm'])<=tolerance and area_error<=.001 and volume_error<=.001
    data={'passed':passed,'distance_tolerance_mm':tolerance,'metric_tolerance':.001,'same_topology':topology,
          'candidate_to_reference':forward,'reference_to_candidate':reverse,'area_difference_mm2':area_error,'volume_difference_mm3':volume_error,
          'reference_geometry':b,'method':'bidirectional_edge_and_trimmed_face_interior_sampling_plus_metrics','continuous_hausdorff_bound_proven':False}
    if not passed:raise RuntimeError('Exact geometry comparison failed: '+json.dumps(data))
    return data
