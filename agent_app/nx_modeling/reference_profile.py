"""Editable station dimensions reconstructed from the supplied STEP section."""
import math
from .features import FeaturePlan

DEFAULTS={'inner_radius':83.,'X1':20.81175,'X2':49.425541,'X3':52.973414,'X4':23.289295,'X5':7.5,
          'Y1':45.921294,'Y2':8.1765525,'Y3':7.398638,'Y4':20.,'sweep_angle':15.}


def reference_plan(overrides=None):
    params=dict(DEFAULTS)
    params.update(overrides or {})
    r=['inner_radius']
    for i in range(1,6): r.append(r[-1]+'+X'+str(i))
    vertices=[[r[0],'-Y1'],[r[1],'-Y1'],[r[2],'-Y2'],[r[3],'-Y3'],[r[4],'-Y4'],[r[5],'-Y4'],
              [r[5],'Y4'],[r[4],'Y4'],[r[3],'Y3'],[r[2],'Y2'],[r[1],'Y1'],[r[0],'Y1']]
    # Fixed dimensionless arc bulges reproduce the supplied circular transitions.
    numeric=[(83,-45.921294),(103.81175,-45.921294),(153.237291,-8.1765525),(206.210705,-7.398638),(229.5,-20),(237,-20),(237,20),(229.5,20),(206.210705,7.398638),(153.237291,8.1765525),(103.81175,45.921294),(83,45.921294)]
    centers={1:(154.004398,-60.4140141750922),3:(206.6084729,-34.4853180006504),7:(206.6084729,34.4853180006504),9:(154.004398,60.4140141750922)}
    arcs={}
    for i,c in centers.items():
        a,b=numeric[i],numeric[i+1]
        t0=math.atan2(a[1]-c[1],a[0]-c[0]);t1=math.atan2(b[1]-c[1],b[0]-c[0])
        delta=(t1-t0+math.pi)%(2*math.pi)-math.pi
        radius=math.hypot(a[0]-c[0],a[1]-c[1])
        m=(c[0]+radius*math.cos(t0+delta/2),c[1]+radius*math.sin(t0+delta/2))
        dx,dz=b[0]-a[0],b[1]-a[1]
        k=((m[0]-(a[0]+b[0])/2)*(-dz)+(m[1]-(a[1]+b[1])/2)*dx)/(dx*dx+dz*dz)
        ar,az=vertices[i];br,bz=vertices[i+1]
        arcs[str(i)]=[f'(({ar})+({br}))/2-({k:.16g})*(({bz})-({az}))',
                      f'(({az})+({bz}))/2+({k:.16g})*(({br})-({ar}))']
    return FeaturePlan.parse({'kind':'feature_model','name':'parametric_sector','parameters':params,'vertices':vertices,
                              'arc_midpoints':arcs,'operation':'revolve','amount':'sweep_angle'})
