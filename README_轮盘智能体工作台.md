# 二维轮盘智能体：调度、状态、结果评价与本地工作台

日期：2026-11-05。当前目标保持最大 von Mises 应力最小化，复用原九参数模型、优化控制器和 Workbench 原生结果检查。

第四阶段更新：新中心搜索工作流现已接入结构化批次反馈与决策追踪，见 [批次反馈闭环说明](README_批次反馈闭环.md)。旧工作流保持原策略，本页下文描述第三阶段基础能力。

本阶段将已有的自然语言规划与优化工具组织成持久化工作流，并提供本地浏览器界面。可以提交任务、预览预算、显式启动计算、查看阶段进度、恢复同一任务、分析历史运行和查看报告。启动页面、刷新和历史分析均不请求模型或启动有限元。

## 启动应用

双击工程根目录的 `start_disk_agent.cmd`，它会启动隐藏的本地服务并打开默认浏览器。再次双击会复用已启动的同一服务。默认地址：

```text
http://127.0.0.1:8765
```

也可在 PowerShell 中以前台方式运行，便于看日志：

```powershell
Set-Location -LiteralPath 'D:\DSV_20261022_1'
& 'C:\Users\18940\.conda\envs\llm_opt\python.exe' -B -m agent_app --app
```

端口被其他应用使用时，可改用 `--app --port 8766`。双击启动器的服务日志与进程编号在 `agent_app_logs/`。关闭浏览器不会停止任务；服务需要继续运行。前台方式可在空闲时用 Ctrl+C 结束服务。

## 页面操作

1. 输入明确任务，例如“新建优化4个候选”。
2. 点击“预览任务”，检查任务单与目标候选预算。默认关闭“规划 LLM”，用离线规则理解任务。
3. 需要实际计算时，点击“开始真实计算”。它允许原优化 LLM 和 Workbench 执行；同一预览任务会使用原工作流继续执行。
4. 从工作流列表查看规划、校验、计算、分析等阶段及候选进度。失败或中断后，可选择原工作流预览恢复条件，再点击继续计算。
5. 从已有运行列表选择历史 run，查看实测应力曲线、最优参数对应指标、面积变化、成本及工程约束状态。页面上的历史分析默认不重新核验原生文件，并明确显示证据状态。

规划模型与优化执行是两个独立开关。开启“规划 LLM”会增加任务理解阶段的 API 请求；点击真实计算才允许优化求解。文本明确要求“只看计划/先别实算”时，该工作流仍只生成计划，需要用新的明确执行请求创建任务。

## 核心模块

| 文件 | 职责 |
|---|---|
| `agent_app/state.py` | 工作流状态、唯一任务编号、原子保存、revision 检查和跨进程任务锁 |
| `agent_app/supervisor.py` | 规划、校验、环境检查、按批执行、结果分析与完成/失败路由 |
| `agent_app/result_analysis.py` | 从已提交真实记录计算改善量、面积/位移变化、约束状态和模型成本，生成确定性报告 |
| `agent_app/web.py` | 仅监听本机的 HTTP 服务、单后台任务、任务/运行/报告接口 |
| `agent_app/static/index.html` | 中文工作台，任务输入、进度、结果曲线、证据和报告 |
| `agent_app/__main__.py` | 保留旧 CLI，并提供工作流、结果分析和应用入口 |

`tools.execute_task` 新增内部 `on_run_bound` 和 `expected_completed_candidates` 参数。前者在首次昂贵基准评价前持久化真实 run_id；后者拒绝在批次调度后发生的意外进度变化。普通 JSON 任务不接受这两个内部参数，原 TaskSpec 契约保持不变。

## 工作流与预算

```text
自然语言或 TaskSpec
  → planner / TaskSpec
  → 工作流状态持久化
  → 案例与预算校验
  → 环境预检
  → 执行一批（原工具 → 原控制器 → 原 LLM/Workbench）
  → 核验已提交记录并分析
  → 预算内下一批，或生成完成报告
```

默认每批沿用搜索配置的4候选边界。例如6候选任务以目标总数4、6分两次调用现有执行器；若已有1候选，追加3候选仍会复用同一批计划。总体目标在首次校验时固定为绝对总候选数，恢复时不会重复叠加 additional budget。效果没有改善也不会自行增加预算。

任务状态与求解断点分别保存：

```text
agent_workflows/wf_.../state.json          工作流任务、阶段、冻结预算与运行绑定
agent_workflows/wf_.../analysis.json       最新结构化分析
agent_workflows/wf_.../analysis/batch_*.json 每个批次的分析
agent_workflows/wf_.../report.md           中文评价报告
python_llm_runs/run_.../checkpoint.json    原控制器求解进度与已提交结果
```

已保存的工作流不携带真实执行权限，每次恢复仍需显式 live。进程在求解期间退出后，恢复沿用保存的 run_id 与 pending 批次，不使用全局 latest 指针猜测。如果此前调用未成功绑定 run，返回 needs_review，避免盲目认领其他运行。

工作流锁和引擎项目锁继续生效。应用内一次只接收一个后台任务；忙时返回明确提示，不自动排队重复启动。若引擎或工程配置改变，旧工作流的真实恢复会拒绝执行。查询历史状态与分析原有结果仍可单独使用。

## 评价结论与成本口径

报告分别说明：

- 原生记录是否重验、求解是否完成；
- 应力是否优于基准和历史最佳，以及分析窗口内的改善；
- 位移和参数公式近似截面积的变化；
- 已实现搜索规则与尚未实现的工程约束；
- API 请求、token 覆盖情况及 Workbench 评价任务记录。

面积为近似截面积，不等于质量。当前未设置许用应力、质量、寿命等约束；应力下降不代表强度或寿命合格。分析器不根据九个尺寸的同时变化推断已验证热点或物理因果关系。

模型成本只统计原始调用日志：中心策略读取 `search/generation_*/center_api/request_*/proposal_*`，直接提案策略读取 `iterations/iter_*/api/request_*/proposal_*`。每候选目录复制的回答不重复计数。HTTP 尝试按 started 事件计数；usage 明细不再次加到总 token。缺失/损坏日志、失败投递或缺失 usage 会标为未知或部分已知。费用未换算为货币，也不混入规划器日志或其他运行。

报告中的模型成本是绑定 run 的累计值，不是某个工作流新增加的费用。恢复已有 run 时，累计值可能含该工作流开始前的调用。Workbench 的“1基准+4候选”指5个评价任务，不等同底层求解器恰好求解5次。

## CLI 与 Python API

工作流预览（记录状态，不实算）：

```powershell
& 'C:\Users\18940\.conda\envs\llm_opt\python.exe' -B -m agent_app --request '新建优化6个候选' --workflow
```

实际执行增加 `--live`；首次返回的 `workflow_id` 用于恢复和查询：

```powershell
& 'C:\Users\18940\.conda\envs\llm_opt\python.exe' -B -m agent_app --workflow-status 'wf_your_workflow'
& 'C:\Users\18940\.conda\envs\llm_opt\python.exe' -B -m agent_app --resume-workflow 'wf_your_workflow'
& 'C:\Users\18940\.conda\envs\llm_opt\python.exe' -B -m agent_app --resume-workflow 'wf_your_workflow' --live
```

第二条为恢复预览，不会覆盖历史失败状态。已完成工作流再次恢复时返回已保存结果，不重复运行。任务全部评价已提交后发生中断，可以在明确恢复时重验记录并继续后续批次；原引擎状态也会保留在报告中。

对历史运行只做分析，并重新核验原生记录：

```powershell
& 'C:\Users\18940\.conda\envs\llm_opt\python.exe' -B -m agent_app --analyze-run 'run_20261104_220450_b58875bbd565' --verify-native
```

```python
from agent_app.supervisor import run_workflow, resume_workflow, get_workflow_status
from agent_app.result_analysis import analyze_run, render_report

preview = run_workflow('新建优化6个候选')
workflow_id = preview.data['workflow_id']
saved = get_workflow_status(workflow_id)
# resume_workflow(workflow_id, live=True)  # 显式实际执行
analysis = analyze_run('run_20261104_220450_b58875bbd565', verify_native=True)
report = render_report(analysis.data)
```

## 验证与本阶段边界

```powershell
& 'C:\Users\18940\.conda\envs\llm_opt\python.exe' -B -m unittest discover -s agent_app/tests -v
```

测试使用临时工程和替代 provider/FEA 边界，覆盖真实控制器的4+2分批、1+3中断恢复、预算冻结、已完成批次不重复求解、状态原子写入、HTTP应用接口与成本去重。正式4候选运行可用只读分析核验；本次开发不自动增加真实候选。

第三阶段提供本地应用入口、有状态调度及确定性评价。第四阶段已将结构化评价摘要接入新中心搜索工作流的下一批提示词，且不增加独立总结LLM请求。RAG、Sobol/Shapley知识和自由反思对话仍未加入。

后续可以在明确的批次接口上增加知识检索和受约束策略反馈；这需要独立验证反馈是否真正改变搜索决策，以及是否在相同有限元预算下带来收益。

本次验收结果：165项测试，164项通过、1项因Windows符号链接权限跳过；28个受保护的引擎、工程及既有运行文件哈希未改变。正式4候选记录的5个原生结果已只读复核，成本为1次成功优化请求、3057 token；开发期间未新增真实API或Workbench评价任务。

## 第五阶段：性能预测智能体

已新增“性能预测”页签和本地GP影子预测节点，详见 [README_性能预测智能体.md](README_性能预测智能体.md)。每个新候选在FEA前保存事前预测状态，页面只读匹配已提交有限元结果并计算误差。原候选策略、预算、缓存计划与原GP门禁不变，不增加LLM请求。旧记录无事前预测时明确标记缺失，不事后回填；当前真实运行5条样本低于24条门槛，仍显示数据不足。

