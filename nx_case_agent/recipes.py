"""Learned design priors, not LLM weights. Values are in mm/degrees."""
RECIPES = {
 'geom2d': {
  'title':'九参数二维轮盘',
  'description':'用NX12建立内外端厚、中间薄、上下对称的二维轮盘截面，内侧位置83毫米，保留九个可编辑参数，过渡平滑。',
  'parameters':{'inner_radius':83.,'X1':20.81,'X2':49.43,'X3':52.97,'X4':23.29,'X5':7.5,'Y1':45.92,'Y2':8.18,'Y3':7.4,'Y4':20.},
  'bounds':{'inner_radius':[1,1000],**{f'X{i}':[1,200] for i in range(1,6)},**{f'Y{i}':[1,100] for i in range(1,5)}},
  'aliases':{'inner_radius':['内半径','内侧位置'],**{f'X{i}':[f'X{i}'] for i in range(1,6)},**{f'Y{i}':[f'Y{i}'] for i in range(1,5)}},
  'learned':'五段连续径向跨度、四个半厚度、镜像轮廓、两处厚薄过渡；由参考截面提炼的原生关联曲线配方。',
  'simplifications':['采用固定弓高比例圆弧替代参考样条，不保证所有连接处严格相切。']},
 'new160': {
  'title':'单叶片轮盘局部三维模型',
  'description':'用NX12建立带一片叶片的三维轮盘局部扇区，包含轮毂、薄盘和加厚外缘，叶片向外伸出并收窄，主要尺寸参数化。',
  'parameters':{'bore_radius':5.2,'hub_length':56.9,'hub_radius':25.,'rim_radius':71.7,'rim_width':20.8,'web_thickness':9.,'sector_angle':7.66,'tip_radius':105.,'root_chord':20.,'tip_chord':14.,'blade_thickness':2.5,'tip_thickness':1.5,'blade_twist':10.},
  'bounds':{'bore_radius':[1,100],'hub_length':[5,200],'hub_radius':[5,150],'rim_radius':[20,500],'rim_width':[3,100],'web_thickness':[1,40],'sector_angle':[3,45],'tip_radius':[25,700],'root_chord':[3,100],'tip_chord':[1,100],'blade_thickness':[.5,10],'tip_thickness':[.3,10],'blade_twist':[-25,25]},
  'aliases':{'bore_radius':['内孔半径','轮毂内孔半径'],'hub_length':['轮毂长度','轮毂轴向总长度'],'hub_radius':['轮毂外半径'],'rim_radius':['盘体外缘半径','盘体半径'],'rim_width':['外缘宽度','盘体外缘轴向宽度'],'web_thickness':['薄盘厚度'],'sector_angle':['扇区角','扇区角度'],'tip_radius':['叶尖半径','叶尖到轴线的距离'],'root_chord':['叶根弦长'],'tip_chord':['叶尖弦长'],'blade_thickness':['叶片厚度'],'tip_thickness':['叶尖厚度'],'blade_twist':['扭转角','叶片扭转角']},
  'learned':'由new160提炼轮毂—薄盘—外缘—单叶片结构及主要尺度。未给定的叶片截面尺寸和扭转采用可优化的工程初值。',
  'simplifications':['盘体采用简化回转轮廓；叶片采用三截面菱形截面放样，不代表经过气动设计的翼型。','本版叶根通过实体重叠求和连接，未建立叶根圆角特征。']}
}
