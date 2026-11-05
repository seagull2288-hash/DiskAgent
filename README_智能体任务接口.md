# 二维轮盘智能体第一阶段：任务单、校验与控制器适配

日期：2026-11-04。目标继续是最大 von Mises 应力最小化。

本阶段新增独立的 `agent_app` 包，复用现有优化计算链。三个核心文件为：

| 文件 | 职责 |
|---|---|
| `agent_app/schemas.py` | `TaskSpec` 用户任务契约、`ToolResult` 统一返回格式 |
| `agent_app/validation.py` | 固定案例、预算、运行路径与断点兼容性校验 |
| `agent_app/tools.py` | 案例读取、预检、优化适配、只读状态查询 |

`__main__.py` 提供 JSON 命令行入口，`examples/` 提供任务样例。第二阶段现已新增 `planner.py` 和 `--request` 自然语言入口，详见 [自然语言规划器说明](README_自然语言规划器.md)。第三阶段现已增加 [调度与本地工作台](README_轮盘智能体工作台.md)。本页继续说明底层任务契约与同步工具接口。

## 1. 可以立即使用的只读命令

先进入项目目录：

```powershell
Set-Location -LiteralPath 'D:\DSV_20261022_1'
```

读取模型、变量、单位和约束，不调用 API，也不启动 Workbench：

```powershell
& 'C:\Users\18940\.conda\envs\llm_opt\python.exe' -B -m agent_app --case
```

检查当前环境与接口配置，不请求 API、不启动 Workbench；只返回密钥是否配置的布尔值：

```powershell
& 'C:\Users\18940\.conda\envs\llm_opt\python.exe' -B -m agent_app --task agent_app/examples/check.json
```

校验四候选任务，默认只返回执行计划：

```powershell
& 'C:\Users\18940\.conda\envs\llm_opt\python.exe' -B -m agent_app --task agent_app/examples/optimize_4.json
```

这里 `status: validated` 表示任务/案例校验通过，环境预检在 `check` 或实际执行时完成，不表示已经完成仿真。

查询最近一次 Python 运行的已提交状态，不触发任何计算：

```powershell
& 'C:\Users\18940\.conda\envs\llm_opt\python.exe' -B -m agent_app --task agent_app/examples/status.json
```

CLI 的标准输出始终是一份 JSON 结果，运行日志写到标准错误流及现有日志文件。进程退出码为 0（工具调用成功）、1（请求/执行失败）或 130（中断）。状态查询调用成功不代表该历史运行成功，需同时查看 `data.run_status`。

## 2. 真正启动求解

以下命令会请求 LLM 并运行 Workbench。保留现有显式 `--live` 行为：

```powershell
& 'C:\Users\18940\.conda\envs\llm_opt\python.exe' -B -m agent_app --task agent_app/examples/optimize_4.json --live
```

新运行包括一次独立基准及四个候选评价。32 个候选池、4 个入选实算和一次中心请求是不同概念，继续按当前搜索配置执行。若中断，沿用已有控制器保存和恢复规则。

## 3. 指定运行与追加预算

`run_id` 只接受 Python 输出目录中 `run_...` 的目录名，不能填路径，不能使用 MATLAB `.mat`。状态任务不填写 `run_id` 时从现有 `latest_run.json` 解析；恢复任务必须显式选择运行。

编辑 `agent_app/examples/resume_add_20.json`，将示例 `run_id` 替换为已有 Python 运行目录名。省略 `--live` 可先校验，实际续算再加 `--live`。

- `total_candidate_budget` 表示目标总候选数。
- `additional_candidate_budget` 表示在当前已完成数上增加的数量。例如已完成 100 次、追加 20，传入旧控制器的目标总数为 120。
- 两个预算字段互斥，只接受正整数，拒绝布尔值、浮点数和数字字符串。
- 基准求解单独计数。失败/重试计数方式沿用现有控制器，本适配层不自行增加真实求解重试。
- 案例、代码或配置指纹不兼容时，保留原恢复拒绝规则。增加预算不会绕过该检查。

## 4. 给后续规划器使用的 Python API

```python
from agent_app.tools import execute_task, read_case, get_run_status

case = read_case()
plan = execute_task({
    "operation": "optimize",
    "case_id": "disk_2d_current",
    "total_candidate_budget": 4,
})
print(plan.to_dict())

status = get_run_status()  # 只读取最近 Python 运行
```

`execute_task(task, live=True)` 才允许优化任务实际运行。`check/status` 即使传入 `live=True` 也不会请求 API 或启动求解器。适配层先检查任务，然后沿用现有入口的环境预检、认证上下文、带 OS 项目锁的控制器和可选绘图顺序。

统一结果包含 `ok / operation / status / data / error / warnings / schema_version`。只返回白名单事实，不整包导出 checkpoint、API 配置或聊天记录。状态中的最优值从已提交历史计算，并附源记录路径，不混入代理预测值。只读查询不重新认证有限元物理模型，也不将“应力较低”表述为许用强度合格。

## 5. 错误与并发行为

- 保留当前同工程串行执行和原项目锁，不删除现有锁文件。
- 本进程内的适配器真实调用也串行，避免 API 认证环境变量上下文交叉。
- 工程占用、无效断点和求解错误返回结构化错误。
- 绘图失败作为 warning，不抹掉已经保存的优化结果。
- 失败时只返回能确定属于本次调用的运行引用，不把全局最新运行误当成本次结果。
- 尚未提供后台任务队列或 Web 停止 API，当前可沿用控制器的 Ctrl+C 后保存行为。

## 6. 隔离测试

```powershell
& 'C:\Users\18940\.conda\envs\llm_opt\python.exe' -B -m unittest discover -s agent_app/tests -v
```

测试使用临时目录和替代 API/求解边界。计算引擎集成测试会运行原 `controller.run` 的状态与保存流程，但有限元和 LLM 均使用明确标记的测试替身，不在真实工程或真实运行目录写入结果。这些测试不代替实际 Workbench 集成求解。

## 7. 参考边界

架构参考作者的 [TurboAgent 任务规划提示词](https://github.com/wuyueteng/TurboAgent-prompts/blob/master/prompts/A1_task_planning_agent.md) 与 [LLM-TAOF 状态定义](https://github.com/wuyueteng/LLM-TAOF/blob/main/llm_optimizer/state.py)。本适配代码为当前轮盘项目独立实现，沿用 Workbench 真值评价，不使用作者叶片 ViT 模型代替轮盘求解。

## 8. PowerShell 中文输出编码

Python 入口按 UTF-8 输出中文 JSON。若 PowerShell 仍按旧代码页解码原生程序输出，中文 message 会显示为乱码；数值和 ASCII 任务字段可能仍正常，但中文消息或后续管道读取会受影响。

本项目 `.vscode/settings.json` 已增加 `PowerShell-UTF8` 终端配置，并设置 Python UTF-8 环境变量。它只作用于本项目新建的 VS Code 终端；现有终端不会自动更新，不影响正在运行的求解。

在现有终端或其他 PowerShell 窗口中，可以先运行以下会话级设置，再执行原命令：

```powershell
$diskAgentUtf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $diskAgentUtf8
[Console]::OutputEncoding = $diskAgentUtf8
$OutputEncoding = $diskAgentUtf8
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
```

只读复查中文计划输出：

```powershell
& 'C:\Users\18940\.conda\envs\llm_opt\python.exe' -B -m agent_app --request '新建优化，计算4个候选' --plan-only
```

应显示“已生成任务单，执行前仍需通过现有案例、预算与运行校验。”，并保持 `total_candidate_budget` 为 4。此复查不请求规划 LLM，也不启动 Workbench。旧的乱码输出不会自动修复，需要重新执行只读命令查看。

如果将 JSON 结果保存到文件，显式使用 `Set-Content -Encoding utf8`，避免 Windows PowerShell 5.1 默认的重定向文件编码与 UTF-8 JSON 读取方式不一致。
