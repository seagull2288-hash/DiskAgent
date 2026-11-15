# Siemens NX 12 自动建模架构

本模块把“模型生成”作为独立工程能力接入 DiskAgent，并与现有 Workbench 优化解耦。

## 模块边界

1. `agent_app/nx_modeling/schema.py`：CAD 无关、严格校验的建模计划。第一版注册 `disk` 模板。
2. `agent_app/nx_modeling/nx12.py`：NX 12 适配器，只把已校验计划编译为 NXOpen Journal，并调用 `run_journal.exe`。
3. `agent_app/nx_modeling/service.py`：预览、执行、请求记录与原生证据，供网页和 MCP 共用。
4. `agent_app/nx_mcp_server.py`：独立 stdio MCP；提供连接检查、圆盘预览、明确 live 建模。
5. `/modeling`：工作台中的自动建模窗口。

## 自然语言建模

工作台“自动建模”窗口输入例如：`创建圆盘，外径100毫米，厚度10毫米，中心孔径20毫米，孔数4，孔径8毫米，节圆直径60毫米`。选择“本地解析”无需网络；参数回填后需要核对、预览，再执行创建。未写单位的数值按毫米，缺少必要参数或包含不支持的特征时会提示补充。当前只支持圆盘及中心和等分螺栓通孔，不能创建任意零件、倒角或盲孔。

选择“使用已配置的语言模型”会使用项目 API 配置和 `optimization_config.json` 指定的密钥环境变量发送描述，可能产生费用；模型结果仍经过计划校验并需人工核对。MCP 的 `nx_plan_description` 使用本地解析，之后可调用 `nx_preview_disk` 和 `nx_create_disk`。

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
- `request_id` 当前仅用于追踪，不提供幂等去重；超时后应先检查本机任务记录，避免重复执行。

## 本机配置与启动

已验证快捷方式指向 `D:\UG12.0\UG\NXBIN\ugraf.exe`，Journal 执行器为
`D:\UG12.0\UG\NXBIN\run_journal.exe`。工作台沿用原启动方式，访问
`http://127.0.0.1:8765/modeling`。

MCP 独立启动：

```powershell
& '.\.venv-mcp\Scripts\python.exe' -X utf8 -m agent_app.nx_mcp_server --root .
```

MCP 客户端启动服务时须将本机 `SPLM_LICENSE_SERVER` 传入服务环境。NX 子进程会配置安装目录、隔离标准输入；许可不可用时不会生成模型。

2026-11-15 在本机使用 MCP stdio 实际创建圆盘：直径 100 mm、厚 10 mm、中心孔直径 20 mm、4 个直径 8 mm 的节圆孔。重新打开 `.prt`，NX 报告 1 个实体、8 个面、6 个圆柱特征；特征表达式与上述尺寸一致。该验收不代表与 Workbench 的自动优化闭环已完成。

## 后续扩展

- 新模板：在 schema 中增加独立计划类型，并注册对应编译器，不修改工作台核心。
- 自动优化：优化器提出参数 → `preview` → NX 创建候选几何 → Workbench 求解 → 指标回写。
- 生产化：把同步 NX 执行接入现有持久任务队列和幂等请求映射；增加 STEP 导出与截图证据。
