# DiskAgent Workbench MCP

第一版已将现有轮盘工程能力封装成标准 MCP stdio 工具。参考项目为 [ANSYS-Workbench-mcp](https://github.com/hongwenwang36-eng/ANSYS-Workbench-mcp)，固定参考提交、MIT许可证和实际复用内容见 [来源记录](third_party/ANSYS-Workbench-mcp/PROVENANCE.md)。上游作者测试的是2025 R1；本项目继续使用现有2020 R2求解脚本，不能据此宣称其他上游工具兼容v202。

```text
支持MCP的对话客户端（如Codex）
  → MCP工具（stdio，独立Python环境）
  → 本机DiskAgent工作台（HTTP 127.0.0.1:8768）
  → 共用任务队列 / 请求去重 / 任务落盘
  → 现有工作流与控制器 / 工程项目锁
  → 原Workbench Journal / 原生Mechanical校验
  → 任务编号、尺寸、应力、质量/频率证据、报告
```

## 网页工程助手

新版工作台使用“工程助手”统一接收任务和对话。明确的新建、追加、续算及环境检查命令由规则解析，经真实MCP工具生成预览或读取体检结果。分析和复杂操作通过独立进程调用对话模型，调用路径为网页消息 → 对话模型工具调用 → MCP stdio → 当前工作台工程接口 → 原有任务队列、Journal与Workbench。

按现有命令启动当前版本的工作台后即可使用。已运行的旧服务需要在空闲时重启，或者为新版服务选择另一个端口。对话客户端自动连接本工作台的实际端口。对话进程使用已有`.venv-mcp`环境，当前支持项目配置的Chat Completions兼容模型，并单独处理`tool_calls`。

可以依次输入以下消息。

1. “读取当前轮盘模型，列出九个变量的参考值与范围。”
2. “读取选中运行的结果，说明最小应力方案和当前配置推荐方案。”
3. “使用参考尺寸，仅将X1增加0.2 mm，预览一次指定设计评价。”
4. 检查预览卡中的完整尺寸、采集配置与任务数量，点击“执行此预览”，也可以输入“执行当前预览”。

左侧选中的运行和已启用的目标约束配置会随消息提供给对话。指定尺寸评价的预览显示1个设计评价任务、0个基准评价。新建优化和续算分别显示待完成候选数与额外基准评价数。计算内部的静力更新、模态求解与重试可能包含多个求解步骤。

左侧保留任务与运行列表，主区域集中显示绑定运行、历史对话和输入框。自动恢复位于“任务设置”。历史对话会恢复保存的运行与目标配置，显式选择新任务会清除运行绑定。调整运行、目标约束或自动恢复设置后须重新生成预览，提交端同时核对保存的设置。续算预览将追加预算换算成核验后的目标总候选数并保存。工作流恢复也在工程助手中生成预览。

工程任务提交后，页面显示任务编号和进度，程序负责轮询。任务完成时，对话通过MCP读取结果并调用模型解释一次。再次发送消息可查询已有结果。对话模型的请求和tokens显示在对话栏，优化模型成本在原运行报告中记录。

每条消息最多6次对话模型请求、8次模型选择的工具调用。页面提供1000至100000的token预算，默认12000。请求发送前按消息和工具结构的UTF-8字节规模保守估算，结合已经返回的用量决定是否继续，并限制本次输出。实际计费以供应商usage为准。工具清单按请求内容选择，模型使用最近四条历史摘录和压缩后的工具结果。完整调用轨迹保存在`agent_chats/chat_*/turns/`。

真实执行采用预览中保存的参数和稳定请求编号，同一预览重复执行返回原任务。新增消息会更新可执行预览，前一份预览作为对话上下文保留。固定结果卡直接读取测量值、约束判定、配置版本和原生证据，缺少基准的相对约束保持数据不足状态。

对话记录存放在`agent_chats/`，可以从“历史对话”选择已有记录，或通过`#chat=chat_编号`打开指定对话。服务重启后点击“恢复任务查询”，系统按原请求映射找回任务，并核验已经完成的原生结果。原任务仍由另一工作台执行时，查询会核对该服务的实例身份并读取原状态。完成结果可从已保存的评价产物或终态工作流恢复。

对话处理和工程求解使用独立执行通道，读取与预览可以在已有工程任务运行时使用。恢复查询与结果解释沿用原任务编号，执行次数以工程记录核对。

## 六阶模态与网格验证

新生成的模态结果同时保存`modal_frequencies_hz`六个值，`frequency_hz`继续表示配置关注的阶次。原生记录包含阶次、分析名称、预应力来源与新鲜度证据。JSON、历史CSV的`F1_Hz`至`F6_Hz`、报告和方案表均提供六阶记录。旧记录缺少六阶数据时显示未采集。

“网格细化验证”面板读取独立工程计算记录。默认网格为0.5、0.4、0.3 mm，默认设计为参考尺寸和X1增加0.2 mm的验收候选，共6个设计评价任务。各案例有独立工程副本，源工程以哈希核对。数值验收相对容差默认待配置，报告分别展示应力、六阶频率和相邻网格变化。

```powershell
& 'C:\Users\18940\.conda\envs\llm_opt\python.exe' -X utf8 -B -m agent_app.mesh_study
& 'C:\Users\18940\.conda\envs\llm_opt\python.exe' -X utf8 -B -m agent_app.mesh_study --live
```

`--mesh-sizes`接受由粗到细的三个尺寸，`--designs`接受含label和parameters的设计JSON列表，`--relative-tolerance`使用相对比例，`--study-id`用于接续已有验证。已提交案例会先核验原生证据再复用。记录位于`python_llm_runs/mesh_study_*/`，包含`study.json`、`mesh_results.csv`和`网格验证报告.md`。MCP工具`disk_read_mesh_study`可读取最新验证结果。

新增模块包括`mcp_client.py`、`chat_worker.py`、`chat_agent.py`和`static/chat.js`。完整链路测试使用真实网页HTTP接口、独立对话进程和MCP SDK，只在模型供应商和原生求解器边界提供测试实现。

## 工具与用途

| 工具 | 用途 |
|---|---|
| `disk_connection_status` | 检查工作台连接、版本、当前任务和工程能力 |
| `disk_check_environment` | 体检本地环境、存储、任务配置与可选续算证据 |
| `disk_read_case` | 读取已注册二维模型、九个变量、范围、映射和证据限制 |
| `disk_list_records` | 列出运行或工作流，默认最多20条 |
| `disk_validate_design` | 校验指定尺寸并预览，零API/FEA调用 |
| `disk_evaluate_design` | 直接评价一组指定尺寸，不调用LLM，不重算基准，不写优化历史 |
| `disk_submit_optimization` | 通过TaskSpec新建或续算现有优化，沿用当前搜索策略和质量/频率配置 |
| `disk_resume_workflow` | 继续已保存工作流，沿用冻结的预算 |
| `disk_get_job` | 查询异步任务及其完整结果 |
| `disk_get_run_status` | 查询已提交进度、最小应力及考虑约束的推荐方案 |
| `disk_get_workflow_status` | 查询持久工作流、预算、恢复和绑定运行 |
| `disk_read_report` | 读取报告结论、约束、推荐尺寸与成本，省略长样本列表 |
| `disk_read_search_diagnostics` | 本地读取搜索/GP诊断，不调用API或Workbench |
| `disk_read_mesh_study` | 读取网格细化计算、六阶频率、变化幅度与容差配置 |

## 本机运行

工程工作台继续使用原来的`llm_opt`环境。MCP SDK安装在独立`.venv-mcp`环境，以免改变求解环境依赖。首次安装：

```powershell
Set-Location -LiteralPath 'D:\DSV_20261022_1'
& 'C:\Users\18940\.conda\envs\llm_opt\python.exe' -m venv '.venv-mcp'
& '.\.venv-mcp\Scripts\python.exe' -m pip install -r requirements-mcp.txt
```

本机本次部署使用8768端口（8765–8767已有旧服务）。已启动的8768新版无需重复启动；下次启动可用：

```powershell
& 'C:\Users\18940\.conda\envs\llm_opt\python.exe' -X utf8 -B -m agent_app --app --port 8768
```

MCP客户端会自动启动`agent_app/mcp_server.py`，无需手工再开一个MCP终端。通用客户端示例见`mcp.config.example.json`。端口更换时修改配置的`--app-url`。启动MCP服务本身不调用LLM或Workbench；它不会自动启动或重启工程工作台。

Codex注册命令（按[官方MCP文档](https://developers.openai.com/codex/mcp/)）：

```powershell
codex mcp add diskagent_workbench -- 'D:\DSV_20261022_1\.venv-mcp\Scripts\python.exe' -X utf8 -B 'D:\DSV_20261022_1\agent_app\mcp_server.py' --app-url 'http://127.0.0.1:8768'
codex mcp get diskagent_workbench
```

配置写入本机Codex配置，不放密钥；现有优化API密钥仍由工程工作台原有机制读取。若当前对话尚未发现新工具，重新打开Codex或新建对话后检查MCP工具列表。工程网页内的原规划对话不会因此自动变成通用MCP客户端。

## 可直接在支持MCP的对话中输入

只读验收：

> 使用diskagent_workbench读取当前轮盘模型的九个参数及范围，再读取最近一次100候选运行的结果报告，说明最小应力方案和满足质量约束的推荐方案。只读取，不调用API或Workbench。

参数预览：

> 读取当前模型，以参考尺寸调用disk_validate_design，只校验并展示参数和预计计算任务数。

明确的一次真实评价：

> 读取当前模型，使用参考尺寸进行一次独立真实仿真，最多1个Workbench评价任务，不调用LLM优化，不追加优化历史。使用固定request_id，提交后查询原任务直至完成，报告应力与实际采集到的质量/频率。

要采集质量或频率，传入与你现有任务相同的已验证`optimization_policy`；没有配置的指标不可声称已采集。单设计评价没有基准求解，依赖基准的相对约束因此不做可行性认证。

优化TaskSpec示例：

```json
{"task":{"operation":"optimize","total_candidate_budget":1},"live":false}
```

续算到总计100个：

```json
{"task":{"operation":"optimize","run_id":"run_实际运行ID","total_candidate_budget":100},"live":false}
```

`live=false`默认只验证任务，零API/求解。真实提交须`live=true`并提供稳定的`request_id`；候选预算不包含新运行的基准评价，控制器重试和内部模态求解也不能等同于候选数。规划LLM保持关闭，优化器内部是否调用LLM按原搜索策略执行。

## 任务可靠性和证据

- 浏览器和MCP共用工作台单任务队列，另由原有跨进程项目锁保护Workbench工程。已有计算时新任务返回`WEB_BUSY`。
- 同一个`request_id`、同样参数重复提交会返回原`job_id`；参数改变则`REQUEST_CONFLICT`。网络超时不得自动换编号重新求解。
- `agent_jobs/job_*.json`记录排队、开始、工作流绑定和完成结果。请求映射保存在`agent_jobs/requests/`。重启后完成结果可读；未完成的旧会话任务显示`detached`，需检查工作流/原生结果，不自动重放仿真。
- 单设计评价存放在`agent_jobs/evaluations/`，与优化checkpoint分开。失败保留请求、Journal、日志等已经生成的证据；原控制器的有界自动恢复用于优化任务，可显式开启，单设计评价不自动重试。
- 使用上游适配的产物状态辅助函数，并继续校验本次Journal完成标记、参数CSV、原生报告新鲜度、物理条件和已配置质量/频率。求解器退出码0本身不代表成功。
- 服务仅通过stdio与客户端通信，内部HTTP固定127.0.0.1，保留Host/Origin/CSRF检查；不接受任意脚本或工程路径。
- MCP适配层主要位于`agent_app`；六阶模态采集、原生核验等扩展同时涉及`python_control`。已有运行能否续算仍以其原有版本与配置校验为准，不绕过引擎指纹检查。

## 第一版范围

当前支持已注册二维轮盘和已验证模态项目的读取、指定参数评价、预算内优化与结果查询。任意新模型的自然语言建模、通用CAD生成、材料/载荷自动设置还没有实现。下一阶段可以在同一工具接口后增加“模型模板注册→生成独立工程→校验模型/网格/载荷→评价”能力，复用本次任务生命周期。

## 验证命令

```powershell
& 'C:\Users\18940\.conda\envs\llm_opt\python.exe' -X utf8 -B -m unittest agent_app.tests.test_mcp_integration -v
& '.\.venv-mcp\Scripts\python.exe' -X utf8 -B -m unittest discover -s mcp_tests -v
```

前者使用真实HTTP/任务队列及原校验流程，在原生求解边界替换假求解器；后者用真实SDK启动stdio子进程，检查initialize、工具发现、调用和错误。二者不消耗优化API额度，也不触发Workbench。

本机2026-11-08验收：完整工程测试455项，454通过、1项跳过；另有2项真实stdio协议测试通过。通过真实MCP→8768工作台读取了九参数、预览任务和`run_20261108_121942_922f2cc4a049`的100候选报告，推荐第98轮，应力633.932520MPa、质量27.612156kg、频率1084.052875Hz。验收前后运行记录一致，没有新增API请求或Workbench评价。记录位于`agent_app_logs/mcp_acceptance_20261108.json`，日志目录不纳入Git。

2026-11-09已完成一次真实网页对话→MCP→Workbench评价。验收候选X1为21.013 mm，其余为参考尺寸，结果为应力739.121951611 MPa、质量36.130306573 kg、第一阶预应力频率848.442697990 Hz，原生核验通过。同一请求重复提交返回原任务，原优化断点哈希核对通过。记录位于`agent_app_logs/six_step_acceptance_20261109/phase1_native.json`。

同日完成参考设计和验收候选在三档网格下的6个真实案例。应力相邻网格变化较小，频率变化幅度仍需进一步核对容差和收敛性。具体数值与原生证据见`python_llm_runs/mesh_study_20261109_validation/网格验证报告.md`。

本轮最终回归586项：585通过、1项跳过；真实MCP SDK/stdio协议测试4项全部通过。重启后的恢复查询返回原任务，未新增对话模型费用；原优化断点哈希仍一致。7个真实设计评价的原生记录重新核验通过，汇总记录位于`agent_app_logs/six_step_acceptance_20261109/final_acceptance.json`。这些检查不代表工程强度认证或频率网格收敛验收；网格验收容差仍待配置。
