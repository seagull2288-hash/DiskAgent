"""Verified geometry assets and dimensions that genuinely drive NX features."""
VERSION='3.0'
ASSETS={'geom2d_boundary.json':'9e8269e2e69c044e3ebb0acfca287ff63a0709907cdbcfa1a6ad17b522ce9f72',
        'new160.x_t':'9004b52a1b0000718b6f75faee446ef2f0a64253e452f439f88b3403e82bfd1b'}
SECTION_DEFAULTS={'inner_radius':83.,'X1':20.81175,'X2':49.425541,'X3':52.973414,'X4':23.289295,'X5':7.5,
                  'Y1':45.921294,'Y2':8.1765525,'Y3':7.398638,'Y4':20.}
SECTION_NOMINAL={'inner_radius':83.,'X1':20.81,'X2':49.43,'X3':52.97,'X4':23.29,'X5':7.5,'Y1':45.92,'Y2':8.18,'Y3':7.4,'Y4':20.}
THREE_D_DERIVED={
    'bore_radius':('5.2*radial_scale',5.2,'内孔半径'),
    'tip_radius':('105*radial_scale',105.,'叶尖半径'),
    'hub_radius':('27.5*radial_scale',27.5,'轮毂大台阶半径'),
    'rim_width':('20.8*axial_scale',20.8,'外缘轴向宽度'),
    'web_thickness':('9*axial_scale',9.,'薄盘局部厚度'),
    'blade_height':('tip_radius-rim_radius',33.32,'叶片高度')}
ALIASES_3D={
    'bore_radius':['内孔半径','轮毂内孔半径'],'hub_length':['轮毂长度','轮毂轴向总长度'],
    'hub_radius':['轮毂外半径','轮毂大台阶半径'],'rim_radius':['盘体外缘半径','盘体半径'],
    'rim_width':['外缘宽度','外缘轴向宽度','盘体外缘轴向宽度'],
    'web_thickness':['薄盘厚度','薄盘局部厚度'],'sector_angle':['扇区角','扇区角度'],
    'tip_radius':['叶尖半径','叶尖到轴线的距离','叶尖到轮盘轴线的距离'],
    'root_chord':['叶根弦长'],'tip_chord':['叶尖弦长'],'blade_thickness':['叶片厚度','叶根厚度'],
    'tip_thickness':['叶尖厚度'],'blade_twist':['扭转角','叶片扭转角'],
    'root_fillet':['叶根圆角','叶根圆角半径'],'blade_height':['叶片高度']}
RECIPES={
    'geom2d':{'title':'精确二维轮盘截面','parameters':SECTION_DEFAULTS,
        'bounds':{'inner_radius':[1,1000],**{'X'+str(i):[1,200] for i in range(1,6)},**{'Y'+str(i):[1,100] for i in range(1,5)}},
        'aliases':{'inner_radius':['内半径','内侧位置'],**{n:[n] for n in SECTION_DEFAULTS if n!='inner_radius'}},
        'learned':'原始STEP完整非均匀B样条节点与控制点；14条边；九参数关联重建。',
        'notes':['默认重建原始边界，不做圆弧、Bezier或镜像近似；保留源文件微小上下差异。','九参数修改按分段坐标映射驱动完整控制网。']},
    'new160':{'title':'精确单叶片轮盘局部','parameters':{'hub_length':56.9,'rim_radius':71.68},
        'bounds':{'hub_length':[50.,65.],'rim_radius':[65.,80.]},'aliases':ALIASES_3D,
        'learned':'保留原始Parasolid的44个面、122条边、全部台阶、叶片与叶根曲面及裁剪边界。',
        'notes':['三维采用内置原始B-rep实例化，保留精确几何；未恢复原作者的草图/回转/放样特征历史。','目前真实可编辑参数为轮毂总长与盘体外缘半径，分别驱动轴向和径向整体缩放；孔径、盘缘宽度和叶高等关联变化。','叶片弦长、扭转、独立薄盘厚度和独立叶根圆角尚不能单独驱动；不会用简化几何替代。']}}
