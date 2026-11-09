# 搜索中心 + Python 采样 + 代理筛选

2026-11-08 已接入 hybrid_local_v3，工作台和 Python 入口默认采用多尺度方向精修、分阶段探索和独立验证的局部GP。历史 v2 实现与配置读取保留。

## 现在的工作流程

1. Workbench 计算参考设计，建立本次运行的真实基准。
2. DeepSeek 根据真实历史返回一个九维搜索中心，JSON 根字段为 center、rationale、candidate_type。
3. Python 生成 32 个分组候选，分别来自合法未测的 LLM 中心、历史最优附近的局部范围、全局均匀采样。
4. 保留原始毫米数值、四位小数、变量范围和去重规则。
5. 每批保护 1 个合法未测的 LLM 中心，其余名额分配给方向精修及本代探索。前8批每批1个全局点，之后通常每3批1个，连续3批停滞时触发一次探索脉冲。中心已测时将其名额补给局部点。每批至少保留一个方向试探名额，经过验证的局部GP可对其余局部名额排序。
6. 4 个候选依次进入同一个 Workbench 工程求解。当前工程不并行写入。
7. 每完成一个候选就保存真实尺寸、应力、位移、最佳方案和断点。
8. 一批完成后重新请求搜索中心，并用最新实测历史重新验证/拟合代理。

预测值从不写入实测历史或替代最佳应力。实际最佳值仍来自经过核验的 Workbench 原生结果。
搜索中心可以等于历史最佳；程序生成的最终候选必须与实测历史不同。

## 两个配置文件

总真实评价预算仍在：

`D:\DSV_20261022_1\python_control\optimization_config.json`

candidate_count 提供默认真实候选预算，工作台请求或命令行 --rounds 可显式指定本次预算。新运行另外计算一次参考设计。

新搜索配置在：

`D:\DSV_20261022_1\python_control\search_config.json`

| 配置项 | 当前值 / 含义 |
| --- | --- |
| strategy | center_surrogate；其他可选 direct（旧单点方式）、center_only（采样但不用代理） |
| selection_policy | hybrid_local_v3；历史 hybrid_local_v2 与 legacy_v1 可识别，读取旧配置保留旧签名形状 |
| seed | 20261029；控制 LHS、候选采样和验证拆分 |
| initial_lhs | 0；直接验证 LLM 中心接口。可设 12，先做 12 个 LHS 实测样本，计入 candidate_count |
| batch_size | 4；每代实际送 Workbench 的候选数 |
| pool_size | 32；每代在 Python 中生成的候选池大小，不代表 32 次求解 |
| sigma_start / sigma_min | v3 局部盒半径为0.08至0.002，按变量跨度归一化。坐标候选使用半径的1/4、1/2、1倍步长 |
| sigma_decay | legacy_v1 中每代乘 0.9；v2 半径由实测批次适应规则调整 |
| global_fraction | v2 候选池全局分组目标比例 0.2，程序同时保底满足真实全局名额 |
| exploration_slots | 1；v3按阶段调度实际名额，本代可以为0。显式配置0可关闭全局探索 |
| surrogate_min_samples | 24；不足时不启用代理，达到后也必须通过验证 |
| surrogate_max_samples | 160；超过时取按时间均匀分布的子集 |
| surrogate_folds | 4；v3在较早训练前缀中进行滚动验证与OOF误差校准 |
| surrogate_max_cv_nrmse | 0.5；CV RMSE / 较早训练样本应力标准差的上限 |
| surrogate_max_rmse_mpa | 15 MPa；交叉验证及最后 20% 留出误差均不能超过该值 |
| surrogate_uncertainty_weight | 1.5；用于最小化目标的均值减不确定性打分 |
| surrogate_max_distance | 0.6；v3还要求候选位于经验证的局部支持盒内，并满足局部训练样本距离限制 |

这些是首版可调参数，不是已证明适用于所有几何的最佳设置。
temperature 仍由 API 配置决定；它与上述尺寸采样 sigma 不同。
初始预算为 1 时只能测试一次中心提议和一次候选求解，代理不会启用。

## 自适应局部范围与版本

局部锚点取冻结批次历史中的最优真实方案，多目标任务遵循已配置目标和约束。每连续2个完整批次没有改善，半径乘0.5。每连续2个完整批次改善，半径乘1.5。v3默认盒半径对应每维0.02至0.8 mm，九个变量的跨度均为10 mm。内部1/4步长最小可达0.005 mm。这些步长属于算法设置，工程改善阈值仍需重复求解和网格核验。

批次改善使用数值容差 max(1e-9 MPa, abs(best_before) × 1e-6)。只有已提交的完整批次参与调整。半批续算继续复用原计划，求解失败的尝试不会被当成已测无改善批次。

v3至少需要batch_size=2，配置的exploration_slots可以为0。每批的完整计划、预算尾批授权和已提交来源分别记录。局部坐标候选保留其他八个参数，方向随完整批次轮转。可行边界采用截断或跳过零位移，稀疏双轴方向用于补充候选池。

策略与实现进入运行指纹，v3应建立新运行和新工作流。历史100候选可在工作台查看，并通过离线模型比较检查。跨版本续算由兼容性检查拒绝，原版本源文件备份位于 [修改前快照](C:/Users/18940/Documents/Codex/2026-10-30/new-chat/outputs/search_v2_before_20261107)。

先在工作台输入“新建优化12个候选，最小化最大应力”并预览。12 个候选按当前分组计划进行功能验收，另外计算参考设计。规划 LLM 可继续关闭。输入新建请求后，预览应显示 mode=new、additional_candidates_to_complete=12。

## 检查与启动

检查环境，无 API、无 Workbench：

```powershell
& "$env:USERPROFILE\.conda\envs\llm_opt\python.exe" -u -B 'D:\DSV_20261022_1\run_python_optimization.py' --check
```

准备真实运行时，以下命令临时指定 100 次候选评价，不修改配置文件：

```powershell
& "$env:USERPROFILE\.conda\envs\llm_opt\python.exe" -u -B 'D:\DSV_20261022_1\run_python_optimization.py' --live --rounds 100
```

initial_lhs=0、batch_size=4 时，100 次评价需要约 25 个搜索中心请求；
格式修正或网络重试可能额外产生请求。参考设计另算一次求解。
若 initial_lhs=12，则 12+22×4=100，约 22 个中心请求。
不足一整批的预算会只评价该批前几个设计；增加总预算并续跑可复用尚未用完的批次。

原 PowerShell 启动器继续可用，自动读取新的搜索策略。
此次代码和策略文件均进入断点指纹；旧版本运行目录不能直接跨版本续跑。
同一新版本的断点仍通过 --live --resume 加原运行目录恢复。
更换 direct / center_only / center_surrogate、修改种子或筛选参数时，应建立新运行。

## 在哪里看模型回答与实际候选

每次运行在 `D:\DSV_20261022_1\python_llm_runs\run_...` 下保存：

| 文件 | 含义 |
| --- | --- |
| search_config.json | 本次搜索配置快照 |
| search\generation_001\center.json | DeepSeek 已校验的搜索中心及原始 API 输出位置 |
| search\generation_001\center_api\request_*\proposal_*\prompt.json | 完整提示词 |
| 同一 proposal_* 下 request.json / response.json / answer.json | 真实请求体、响应及中心回答；认证密钥不落盘 |
| search\generation_001\plan.json | 完整候选池、选中索引、代理验证指标与明确标记的预测值 |
| iterations\iter_001\candidate.json / selection.json | Python 实际选中的尺寸及来源 |
| iterations\iter_001\answer.json | 对应的模型中心回答，同一批可能共享同一回答，不表示重复调用 |
| iterations\iter_001\result.json | 真实求解结果 |
| history.csv / best_design.json | 只包含真实测量值的历史和最优方案 |

终端会显示 SEARCH source、generation、surrogate、reason。
source=python_sample 表示程序批次选择，source=initial_design 表示 LHS。
v2 的 selection_origin 记录具体来源，llm_center 表示模型给出的新中心，best_local 表示最优附近局部新点，global_uniform 表示全局生成分支。
surrogate=False 是样本数或误差未达标等情况下的正常回退。

## 代码分工与对照实验

| 文件 | 职责 |
| --- | --- |
| python_control\search.py | 中心提示词、返回校验、批次计划、来源追溯和恢复 |
| python_control\sampling.py | 归一化、高斯反射、LHS、四位小数与去重 |
| python_control\surrogate.py | NumPy 高斯过程、CV、独立留出验证与保留探索的筛选 |
| python_control\local_search.py | 分组候选池、未测中心保护、已测最优锚点和完整批次自适应半径 |
| python_control\directional_search.py | v3多尺度坐标方向、可行边界处理、动态全局名额和确定性候选池 |
| python_control\local_surrogate.py | NumPy Matérn 5/2 ARD与噪声学习、局部支持域、滚动验证和OOF不确定度校准 |
| agent_app\search_diagnostics.py | 核验历史后进行离线模型比较、来源统计与重复性证据检查 |
| python_control\controller.py | 沿用主循环，逐点保存真实结果和断点 |

环境已有 NumPy，不需要另外安装 scikit-learn 或大型神经网络框架。
原 api_bridge\llm_bridge.py 的 HTTP、认证与请求日志实现继续复用，原单点接口保留。
当前几何与工程约束未增加；许用应力、固定外径等仍需用户明确设计要求后另行定义。

筛掉 28 个未评价的池内候选，不等于已经证明节省了 28 次真实求解。
应以相同初始条件/真实预算，对照 center_only 与 center_surrogate 到达同一应力水平
所需的真实评价数，并做独立重复试验。direct 可作为原单点方法对照。

## v3局部GP与离线验收

局部GP采用Matérn 5/2核，各变量具有独立长度尺度，并在训练数据上估计噪声。训练支持盒由训练前缀中的最优点与预先配置的sigma_start确定。每个时间验证折使用自己的训练前缀，最终向前留出块只用于验收。

- 外层局部训练至少24个不同设计，滚动训练折至少12个。
- 滚动OOF与最终局部向前验证各至少8点。
- 应力CV RMSE和向前RMSE均至多15 MPa，CV NRMSE至多0.5。
- 前向预测与实测的Spearman排序相关至少0.30，报告同时展示预测前四分之一候选的实测遗憾值。
- 标准差采用训练期OOF残差膨胀与绝对下限校准。最终95%名义区间的经验覆盖率至少0.80，小样本经验覆盖率仍需未来数据确认。
- 候选必须同时位于保存的支持盒内并满足最近局部训练距离，才具备代理排序资格。多目标任务对所需指标分别验证。

性能预测页增加“离线模型比较”。用户点击后读取原生核验通过的历史，比较原RBF、全局Matérn ARD和局部Matérn ARD。表格分别展示训练/验证数量、滚动误差、同一局部范围的前向误差及覆盖率，原RBF原始门控结果保留在详细报告中。接口执行本地数值运算，API与FEA预算均为0。

CLI同样提供只读入口。

```powershell
& 'C:\Users\18940\.conda\envs\llm_opt\python.exe' -X utf8 -B -m agent_app --search-diagnostics 'run_20261107_140454_fbe5079f3ab1'
```

重复性诊断会检查历史中完全相同尺寸的重复测量。缺少重复记录时报告repeat_measurements_required，工程停滞阈值保持待核验状态。实际重复FEA及新的优化实验仍由用户显式启动。
