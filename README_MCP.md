# DiskAgent：API / 外部 AI 两种模式

API 模式保留原有规划、优化、GP 筛选、批次反馈与 Workbench 链路，内部模型 API 仍计费。
外部 AI 模式由 MCP 客户端负责提出候选和分析结果；新增工具不调用内部模型 API。
外部模式第一版提供单候选评估，不自动复用内部 GP/批次优化闭环，也不把外部记录伪装成原控制器 checkpoint。
聊天软件的套餐额度取决于该软件；本接口不会转移 API 额度或自动登录 ChatGPT。

## 安装与启动

Python 3.10+；在项目根目录创建 `.venv` 后执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt
.\start_disk_agent.ps1
```

网页左侧“优化驱动方式”可选择 API 或外部 AI / MCP，选择保存在当前浏览器。
外部面板可读取九参数定义、粘贴 JSON、校验、提交单次真实求解、刷新记录。
关闭网页不停止后台求解；必须保持服务进程运行。

## MCP 本地客户端

参考 `mcp-client.example.json`，其中使用绝对路径入口，不依赖客户端工作目录。终端也可以在项目根目录启动：

```powershell
.\.venv\Scripts\python.exe -B -m agent_app.mcp_server
# 或：
.\start_disk_mcp.ps1
```

工具：`get_case`、`preview_candidate`、`submit_candidate`、`get_candidate_status`、`list_candidates`。
先读取案例；根据变量上下界生成九参数；预览；明确 `live=true` 后提交。
`candidate_id` 为 `candidate_` 加 32 位小写十六进制，例如 UUID 去掉连字符。
同一候选重试必须复用同一 ID；同 ID 不同参数会拒绝。失败也不自动重算，避免重复付出求解成本。
返回编号后轮询状态；完成后读取原生应力 MPa、位移 mm 和证据。工程合格性不由“应力降低”推出。

## HTTP / ChatGPT

```powershell
.\start_disk_mcp.ps1 -Transport streamable-http
```

本机地址：`http://127.0.0.1:8766/mcp`。MCP 与网页共同读取
`python_llm_runs/external_candidates`，并使用原工程 OS 锁，阻止同时修改同一 Workbench 工程。
HTTP 仅监听本机，不内置公网认证。不要直接公开此端口。
ChatGPT 云端不能直接访问你电脑的 127.0.0.1；需要其支持的本地连接机制，或另外部署带认证的 HTTPS 网关。
本次未建立公网隧道、部署网关或绑定 ChatGPT 账号，因此不能声称 ChatGPT 已连接。

## 本机求解条件与恢复

`python_control/optimization_config.json` 是本机配置，沿用原格式；核对项目文件、RunWB2 路径和许可证。
无 API 密钥也可使用外部候选工具；API 模式仍需配置对应密钥。
进程崩溃后 queued/running 记录不会自动重新执行：需先核实 Workbench 是否仍运行及原生结果，再人工处置。
记录保留参数、结果和错误，外部服务不支持任意脚本执行、任意路径读取或修改材料/载荷。

## 验证

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s agent_app/tests -v
```

隔离测试使用求解替身；不能代替本机 ANSYS 真实求解和许可证验证。
