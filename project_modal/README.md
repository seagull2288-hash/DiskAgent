# 质量与工作转速预应力轴对称模态验收

验收日期 2026-11-08。工程已真实求解，并重新打开核对静力、模态状态及六阶结果。

## 可打开的工程

[预应力轴对称工程](D:/DSV_20261022_1/project_modal/2dmodel.wbpj)

该工程保存静力分析与 `DiskAgent Prestressed Axisymmetric` 模态分析。预应力来源指向同一静力工况，六个变形结果节点分别为 `DiskAgent Mode 01` 至 `DiskAgent Mode 06`。

## 质量验收

| 方案 | 最大应力 MPa | 整盘质量 kg | 最大位移 mm |
|---|---:|---:|---:|
| 基准 | 740.121804168 | 36.001760630 | 0.461053422 |
| 验收候选 | 739.121951611 | 36.130306573 | 0.461160855 |

验收候选将X1从20.8130 mm增至21.0130 mm，其余八个变量保持基准值。

本二维面体的Body.Mass与Geometry.Mass返回0。整盘质量根据Mechanical真实截面面积、形心半径及Engineering Data的7850 kg/m³密度计算，旋转体积采用 V = 2π A r。

MAPDL明确按360°轴对称模型汇总质量，求解器单位为tonne，换算后为36.125000 kg。与几何质量的相对差为0.014687%，通过0.1%的一致性门槛。

## 模态工况与结果

工况沿用1349.3 rad/s绕全局Y轴旋转、-131.85 MPa外缘压力、内侧下端点Uy=0支撑、Structural Steel材料及0.5 mm网格。求解器确认采用预应力线性摄动模态，使用Block Lanczos提取前六阶。

以下频率属于上述验收候选，模态范围为轴对称0节径。Coriolis设置为False，Spin Softening设置为ProgramControlled。

| 阶次 | 固有频率 Hz |
|---|---:|
| 1 | 848.442698 |
| 2 | 2806.525759 |
| 3 | 5585.088091 |
| 4 | 6326.079600 |
| 5 | 8846.488156 |
| 6 | 13308.402988 |

## 工作台使用

使用新版服务。应力与质量任务继续使用原静力工程，启用频率后任务绑定这个独立预应力模态工程。模态分析名称保持 `DiskAgent Prestressed Axisymmetric`，可选择1～6阶。先点击预览，核对保存的配置后再执行后续真实任务。

质量、应力或频率的工程限值由任务配置指定。当前频率取值按固定阶次选择，后续涉及模态交叉时需要加入振型跟踪。

## 计算记录

本次包含2次静力设计评价、1次预应力来源静力更新和2次六阶模态分析（含一次生产采集接口复核），LLM API调用为0。原静力工程的wbpj、agdb、mechdb与既有100候选断点已核对哈希。

[完整验收数据](D:/DSV_20261022_1/python_llm_runs/engineering_acceptance_20261108_102344_597227/acceptance_summary.json)

[模态求解原生报告](D:/DSV_20261022_1/python_llm_runs/engineering_acceptance_20261108_102344_597227/modal_solve/mechanical_verified.json)

[求解器模态输出](D:/DSV_20261022_1/python_llm_runs/engineering_acceptance_20261108_102344_597227/modal_solve/modal_solver_snapshot/solve.out)

生产采集接口复核已通过，同一验收候选回读质量36.130306573 kg、一阶频率848.442697990 Hz，确认预应力来源正确，复核期间静力来源保持Done。
