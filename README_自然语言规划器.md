# 二维轮盘智能体第二阶段：自然语言任务规划器

日期：2026-11-04。新增 `agent_app/planner.py`，将自然语言转换为第一阶段已有的 `TaskSpec`，再通过原任务执行器调用优化流程。

2026-11-05 更新：现已增加持久化监督调度、结果评价和本地浏览器应用，使用方式见 [轮盘智能体工作台](README_轮盘智能体工作台.md)。本页继续说明单任务自然语言规划接口。

```text
自然语言请求
  → planner.plan_task：识别意图、预算、运行引用、只规划要求
  → TaskSpec：check / optimize / status
  → validation：案例、预算、断点、指纹校验
  → tools.execute_task
  → 原 controller / 原 API 提案 / 原 Workbench 真值评价
  → ToolResult 结构化结果
```

规划器不改写轮盘的九个几何变量、参数映射、四位小数规则、最大 von Mises 应力最小化目标或求解控制器。作者 TurboAgent 的 A1 提示词提供任务分类、信息补全和工具调用前检查的架构参考；这里为现有轮盘工程独立实现，仍由 Workbench 给出真实评价。

## 已实现的能力

| 请求示例 | 处理结果 |
|---|---|
| 检查环境 | `check`，执行现有预检工具 |
| 新建优化，计算4个候选 | 新建优化，`total_candidate_budget=4` |
| 查询上一次运行状态 / 查看优化结果 | `status`，将最近运行绑定为具体 `run_id` |
| 继续上一次，再算20次 | 在该运行上追加20个候选 |
| 继续上次运行，总共100个候选 | 该运行的目标总候选数为100 |
| 再算20次 | 无运行上下文时返回澄清问题，不自行选择运行 |
| 优化一下 | 返回预算问题，不使用配置中的默认次数补全 |
| 优化20到30个候选 / 优化20代 | 返回澄清，不把区间或代数当作候选数 |
| 只看计划，优化4个候选 / 优化4次，先别实算 | 返回任务单，即使调用方传了 `live=True` 也不执行工具 |
| 修改材料后优化20次 / 生成报告 / 停止任务 | 返回未支持，不截取其中的优化部分执行 |

默认使用离线规则，支持常见明确中文和英文说法，以及阿拉伯数字、全角数字和常用中文整数。规则覆盖范围有限；不确定时返回问题。可显式启用已有 API 的 LLM 规划路径，不会在规则失败后自动联网。

当前每个请求只处理一个任务。“检查环境，然后优化20次”会要求选择一个任务；后续可由调度模块实现任务序列。澄清问题通过 `data.questions` 返回，用户补全后重新提交请求；目前没有自动追问窗口或跨请求对话记忆。

## 立即使用

在 PowerShell 中进入现有工程：

```powershell
Set-Location -LiteralPath 'D:\DSV_20261022_1'
$diskPython = 'C:\Users\18940\.conda\envs\llm_opt\python.exe'
```

只查看自然语言生成的任务单，不执行环境检查、状态工具或求解：

```powershell
& $diskPython -B -m agent_app --request '新建优化，计算4个候选' --plan-only
```

把任务单交给原校验器，查看预算、基准评价数量和执行模式；仍不请求 API 或启动 Workbench：

```powershell
& $diskPython -B -m agent_app --request '新建优化，计算4个候选'
```

读取环境和最近运行：

```powershell
& $diskPython -B -m agent_app --request '检查环境'
& $diskPython -B -m agent_app --request '查询上一次运行状态'
```

预览续算计划：

```powershell
& $diskPython -B -m agent_app --request '继续上一次，再算20次' --plan-only
& $diskPython -B -m agent_app --request '继续上次运行，总共100个候选' --plan-only
```

指定运行上下文时，将以下示例目录名替换为自己实际的 Python 运行名：

```powershell
& $diskPython -B -m agent_app --request '再算20次' --run-id 'run_your_existing_run' --plan-only
```

运行引用优先级为：文本中的具体 `run_id`、文本明确指定的“上次/最近”、`--run-id` 上下文、状态查询的默认最近运行。仅接受输出目录下的运行目录名，拒绝路径、目录跳转和不匹配的断点。LLM 规划期间会固定已绑定的运行，不会因为 `latest_run.json` 更新而悄悄切换对象。

真实求解仍需显式添加 `--live`：

```powershell
& $diskPython -B -m agent_app --request '新建优化，计算4个候选' --live
```

此命令沿用原优化流程，可以调用原提案 LLM 和 Workbench。新建4候选任务还包含一次独立基准评价；候选次数不等于批次数、API次数或包含重试在内的底层求解调用数。断点续算会执行原兼容性和指纹检查。

## 两个独立的联网/执行开关

| 选项 | 任务理解 | 优化任务处理 |
|---|---|---|
| 不加开关 | 离线规则 | 校验后返回计划 |
| `--planner-live` | 可调用配置的规划 LLM | 校验后返回计划 |
| `--live` | 离线规则 | 可进入原优化 LLM 与 Workbench 流程 |
| `--planner-live --live` | 可调用规划 LLM | 可进入原优化流程 |
| `--plan-only` | 离线规则 | 只生成 TaskSpec |
| `--planner-live --plan-only` | 可调用规划 LLM | 只生成 TaskSpec |

`--plan-only` 与 `--live` 不能同时使用。`--planner-live`、`--run-id` 和 `--plan-only` 仅用于 `--request` 模式。原 `--task` JSON 和 `--case` 入口保留。

检查和状态工具本身不请求 API、不启动有限元；若另加 `--planner-live`，前面的自然语言理解步骤可以请求规划 API。文本中的“不要执行/先别实算/只看计划”等要求优先阻止工具执行。

LLM 规划复用现有 API 配置和认证上下文，单次生成的输出上限为1024 token，保留既有 HTTP 重试策略。程序检查模型输出的完整字段、固定案例、固定目标、预算类型/数值和运行引用，拒绝模型编造或改写这些内容。LLM 输出不能打开真实执行开关。

## Python 接口与结果解释

```python
from agent_app.planner import plan_task, execute_request

plan = plan_task('新建优化，计算4个候选')
print(plan.to_dict())

validated = execute_request('新建优化，计算4个候选')
print(validated.to_dict())

# 显式设置 use_llm=True 才请求规划模型。
# 显式设置 live=True 才允许优化器真正执行。
# execute_request('新建优化4个候选', use_llm=True, live=True)
```

`plan_task()` 只生成任务单并检查案例定义和运行引用，不进行优化断点的完整恢复校验，不调用任务工具。`execute_request()` 在计划成功后复用 `execute_task()`，继续进行原预算、兼容性等校验；计划被标记为只规划时直接返回。

| `status` | 含义 |
|---|---|
| `planned` | 已生成任务单，尚未执行任务工具 |
| `validated` | 优化任务已通过原适配层校验，尚未开始实算 |
| `checked` / `status_read` | 环境检查 / 历史状态读取成功 |
| `completed` | 原控制器返回已完成运行 |
| `clarification_needed` | 信息不足或有歧义，问题位于 `data.questions` |
| `unsupported` | 当前未支持的任务，不执行 |
| `failed` / `interrupted` | 结构化错误 / 执行中断 |

状态查询成功不代表历史优化运行成功，需查看 `data.run_status`；`planned` 和 `validated` 都不表示已经获得新的应力结果。

启用 LLM 后，规划记录独立写入 `agent_plans/plan_*`，包括脱敏的提示词、请求/响应、manifest 和 `plan.json`；不混入 `python_llm_runs` 优化历史。离线规则模式不新建规划日志目录，可由上层保存返回的 `ToolResult`。CLI 标准输出为单份 UTF-8 JSON，操作日志使用标准错误流；澄清/失败退出码为1，中断为130，成功为0，命令行参数错误为2。

## 验证与后续扩展

```powershell
& $diskPython -B -m unittest discover -s agent_app/tests -v
```

测试覆盖解析、预算歧义、只规划门控、运行绑定、模型输出漂移、实际 LLM 客户端的模拟 HTTP 传输、日志脱敏和 CLI 开关；另以临时案例运行真实规划器、适配器和控制器的新建—查询—续算链路。API、有限元及原生结果认证边界使用明确标记的替身，不调用真实服务、不在正式运行中写入测试结果。

本次实现完成“任务理解与规划 → 现有优化执行能力”的衔接。后续调度/状态模块可消费统一的 `TaskSpec` 和 `ToolResult`，再加入多步任务、评价反馈与报告模块；目前尚未形成六角色并行协作系统，也没有新增网页应用。

架构参考：[TurboAgent A1 任务规划提示词](https://github.com/wuyueteng/TurboAgent-prompts/blob/master/prompts/A1_task_planning_agent.md)。第一阶段任务契约说明见 `README_智能体任务接口.md`。
