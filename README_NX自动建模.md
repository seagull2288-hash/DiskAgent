# Siemens NX 12 自动建模架构

本模块把“模型生成”作为独立工程能力接入 DiskAgent，并与现有 Workbench 优化解耦。

## 模块边界

1. `agent_app/nx_modeling/schema.py`：CAD 无关、严格校验的建模计划。第一版注册 `disk` 模板。
2. `agent_app/nx_modeling/nx12.py`：NX 12 适配器，只把已校验计划编译为 NXOpen Journal，并调用 `run_journal.exe`。
3. `agent_app/nx_modeling/service.py`：预览、执行、请求记录与原生证据，供网页和 MCP 共用。
4. `agent_app/nx_mcp_server.py`：独立 stdio MCP；提供连接检查、圆盘预览、明确 live 建模。
5. `/modeling`：工作台中的自动建模窗口。

```text
自然语言/工作台/MCP
        │
        ▼
受约束 ModelingPlan ── preview（无 NX 副作用）
        │ live=true + request_id
        ▼
NX12 Adapter ── NXOpen Journal ── .prt + result.json
        │
        └────────► 现有 Workbench 评价/优化闭环
```

## 为什么这样拆分

- 优化器不依赖 Siemens API；未来可并列加入 FreeCAD、Creo 等适配器。
- MCP 不接收任意脚本或任意输出路径，LLM 只能选择已注册模板和参数。
- 默认预览；真实建模要求 `live=true` 和稳定 `request_id`。
- 每次执行保留规范化计划、SHA-256、Journal、stdout/stderr 和产物状态。

## 本机配置与启动

已验证快捷方式指向 `D:\UG12.0\UG\NXBIN\ugraf.exe`，Journal 执行器为
`D:\UG12.0\UG\NXBIN\run_journal.exe`。工作台沿用原启动方式，访问
`http://127.0.0.1:8765/modeling`。

MCP 独立启动：

```powershell
& '.\.venv-mcp\Scripts\python.exe' -X utf8 -m agent_app.nx_mcp_server --root .
```

## 后续扩展

- 新模板：在 schema 中增加独立计划类型，并注册对应编译器，不修改工作台核心。
- 自动优化：优化器提出参数 → `preview` → NX 创建候选几何 → Workbench 求解 → 指标回写。
- 生产化：把同步 NX 执行接入现有持久任务队列和幂等请求映射；增加 STEP 导出与截图证据。
