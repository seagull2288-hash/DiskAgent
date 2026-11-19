# NX12 两案例建模智能体 v2.0

输入不完整的自然语言需求，即可识别九参数二维轮盘或带单叶片的三维轮盘局部模型，从案例配方补全尺寸，创建原生参数化NX零件。当前范围为这两类结构。

“训练完成”在本交付中指案例知识、默认参数、几何关系和生成程序已经固化并经过真实NX验证；没有训练GPT模型权重。此模块无需GPT API密钥，使用有限范围的词语和尺寸解析。接入LLM后可通过MCP使用相同工具。

## 接收方独立使用

需要Windows、Python 3.10+、正常可用的NX12与许可。把完整的 `nx_case_agent` 文件夹放到任意目录，设置本机NX路径：

```powershell
$env:NX_AGENT_NXBIN='D:\UG12.0\UG\NXBIN' # 换成接收方真实路径
# 同时配置本机NX许可所需的SPLM_LICENSE_SERVER
python -X utf8 nx_case_agent/agent.py web --port 8771
```

打开 http://127.0.0.1:8771 ，输入需求，点击“理解描述并补全参数”，核对尺寸和简化说明，再点击建模。也可进入文件夹双击start.cmd。每次执行保存独立任务与PRT，路径在结果中返回。

可以直接使用以下描述：

> 请用NX12建立一个内外端厚、中间薄、上下对称的二维轮盘截面，五段径向跨度和四个半厚度可修改，过渡平滑。

> 请用NX12建立一个带一片叶片的三维轮盘局部模型，包含轮毂、薄盘和加厚外缘，叶片向外伸出并逐渐收窄，主要尺寸参数化。

也支持 `二维轮盘，X1=23，Y2=10` 和 `三维轮盘，叶尖半径110毫米，扭转角12度`。未说明的尺寸来自recipes.py，计划会列出哪些参数由案例补全。

## 放入DiskAgent

本分支已经包含该文件夹和集成代码。重启DiskAgent后访问 `/case-modeling`，或在“自动建模”页点击“两案例建模智能体”。接口为 `POST /api/nx/cases/plan` 和 `POST /api/nx/cases/build`，复用工作台的本机访问与CSRF检查。

向其他DiskAgent版本移植时，复制文件夹后可直接调用：

```python
from nx_case_agent import plan, execute
preview = plan('建立二维轮盘')
result = execute('建立二维轮盘', parameters={'X1':23, 'Y2':10},
                 jobs_root='output/nx_cases')
```

`parameters` 是为后续优化智能体保留的结构化入口，经过范围和几何关系校验。创建候选几何已实现；本模块没有自动把候选模型连接到Workbench求解。

## 接入LLM的MCP配置

可选依赖：`python -m pip install mcp`。

```json
{
  "mcpServers": {
    "nx12_cases": {
      "command": "<Python可执行文件绝对路径>",
      "args": ["<nx_case_agent目录绝对路径>/mcp_server.py"],
      "env": {
        "NX_AGENT_NXBIN": "<NX12安装目录>/NXBIN",
        "SPLM_LICENSE_SERVER": "<接收方本机许可设置>"
      }
    }
  }
}
```

工具：list_learned_models、preview_model、build_model。DiskAgent现有NX MCP服务也新增nx_case_preview和nx_case_build；后者默认预览，live=true时建模。

## 可编辑几何与验证

- 二维：内侧位置加九个设计尺寸，共10个命名表达式；关联点→直线/圆弧→二维面体。
- 三维：13个命名表达式；简化盘体回转→三截面渐缩扭转叶片放样→实体求和。
- 每次生成后，逐个将命名尺寸增加0.05，检查NX更新、几何变化，恢复初始值，再保存并重新打开验证。
- 未提供的叶片截面形状和扭转采用明确的设计初值。二维圆弧不保证各处严格相切；三维采用菱形截面简化叶片，当前未建立叶根圆角。这些差异在预览和结果中列出。

示例PRT和实测记录在 `examples/`。它们属于新生成的简化可优化模型，不是源文件的精确复刻。配方来源见knowledge_manifest.json；接收方无需携带原始STEP或Parasolid文件，正常建模不导入源文件。

开发测试：`python -m unittest nx_case_agent.test_core`。安装mcp后可运行真实协议与NX验收脚本 `python nx_case_agent/acceptance.py`，会生成两个新模型并写入验收记录。
