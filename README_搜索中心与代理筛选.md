# 搜索中心 + Python 采样 + 代理筛选

2026-10-29 已接入当前 Python 主控。本次未发送真实 API 请求或启动 Workbench。

## 现在的工作流程

1. Workbench 计算参考设计，建立本次运行的真实基准。
2. DeepSeek 根据真实历史返回一个九维搜索中心，JSON 根字段为 center、rationale、candidate_type。
3. Python 在归一化空间反射高斯采样，并混入少量全局样本，生成 32 个候选。
4. 保留原始毫米数值、四位小数、变量范围和去重规则。
5. 真实样本不足或代理误差超标时，按空间覆盖选出 4 个候选；
   代理通过交叉验证和独立的时间顺序留出验证后，按预测均值减不确定性权重选点，并保留探索名额。
6. 4 个候选依次进入同一个 Workbench 工程求解。当前工程不并行写入。
7. 每完成一个候选就保存真实尺寸、应力、位移、最佳方案和断点。
8. 一批完成后重新请求搜索中心，并用最新实测历史重新验证/拟合代理。

预测值从不写入实测历史或替代最佳应力。实际最佳值仍来自经过核验的 Workbench 原生结果。
搜索中心可以等于历史最佳；程序生成的最终候选必须与实测历史不同。

## 两个配置文件

总真实评价预算仍在：

`D:\DSV_20261022_1\python_control\optimization_config.json`

candidate_count 当前仍为用户原先设置的 1，本次没有提高。它是新增候选的真实评价数，
不是代数，也不是 API 调用数；另有一次参考设计求解。

新搜索配置在：

`D:\DSV_20261022_1\python_control\search_config.json`

| 配置项 | 当前值 / 含义 |
| --- | --- |
| strategy | center_surrogate；其他可选 direct（旧单点方式）、center_only（采样但不用代理） |
| seed | 20261029；控制 LHS、候选采样和验证拆分 |
| initial_lhs | 0；直接验证 LLM 中心接口。可设 12，先做 12 个 LHS 实测样本，计入 candidate_count |
| batch_size | 4；每代实际送 Workbench 的候选数 |
| pool_size | 32；每代在 Python 中生成的候选池大小，不代表 32 次求解 |
| sigma_start / sigma_min | 0.08 / 0.02，按每个变量的上下限跨度归一化 |
| sigma_decay | 每代乘 0.9，直到 sigma_min |
| global_fraction | 0.2，全局采样概率 |
| exploration_slots | 1；代理启用时仍保留探索名额 |
| surrogate_min_samples | 24；不足时不启用代理，达到后也必须通过验证 |
| surrogate_max_samples | 160；超过时取按时间均匀分布的子集 |
| surrogate_folds | 4；仅在较早 80% 数据中选 GP 长度尺度 |
| surrogate_max_cv_nrmse | 0.5；CV RMSE / 较早训练样本应力标准差的上限 |
| surrogate_max_rmse_mpa | 15 MPa；交叉验证及最后 20% 留出误差均不能超过该值 |
| surrogate_uncertainty_weight | 1.5；用于最小化目标的均值减不确定性打分 |
| surrogate_max_distance | 0.6；限制代理指导的利用点与实测样本的归一化距离 |

这些是首版可调参数，不是已证明适用于所有几何的最佳设置。
temperature 仍由 API 配置决定；它与上述尺寸采样 sigma 不同。
初始预算为 1 时只能测试一次中心提议和一次候选求解，代理不会启用。

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
source=python_sample 表示由程序采样生成，source=initial_design 表示 LHS；
不能把这些尺寸逐字当作 DeepSeek 直接给出的回答。
surrogate=False 是样本数或误差未达标等情况下的正常回退。

## 代码分工与对照实验

| 文件 | 职责 |
| --- | --- |
| python_control\search.py | 中心提示词、返回校验、批次计划、来源追溯和恢复 |
| python_control\sampling.py | 归一化、高斯反射、LHS、四位小数与去重 |
| python_control\surrogate.py | NumPy 高斯过程、CV、独立留出验证与保留探索的筛选 |
| python_control\controller.py | 沿用主循环，逐点保存真实结果和断点 |

环境已有 NumPy，不需要另外安装 scikit-learn 或大型神经网络框架。
原 api_bridge\llm_bridge.py 的 HTTP、认证与请求日志实现继续复用，原单点接口保留。
当前几何与工程约束未增加；许用应力、固定外径等仍需用户明确设计要求后另行定义。

筛掉 28 个未评价的池内候选，不等于已经证明节省了 28 次真实求解。
应以相同初始条件/真实预算，对照 center_only 与 center_surrogate 到达同一应力水平
所需的真实评价数，并做独立重复试验。direct 可作为原单点方法对照。
