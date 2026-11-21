# NX12 精确两案例建模智能体 v3.0

本版修正了简化轮毂和菱形叶片与参考件差距过大的问题。默认生成完整参考几何，包含原始台阶、真实叶片、叶根过渡和裁剪边界。生成失败或精度校验失败时返回错误，不退回简化轮廓。

三维几何精确保留与完整特征级参数化是两项不同能力。目前三维以内置原始Parasolid B-rep实例化，再添加NX原生轴向/径向缩放特征；尚未恢复原作者的草图、回转、放样和圆角特征树，也不支持分别修改叶型、扭转角或局部圆角。二维使用完整控制网和原始节点创建原生关联样条，支持九参数修改。这些状态由API和界面明确报告。

## 独立使用与移交

需要Windows、Python 3.10+、NX12及其有效许可。复制完整的 `nx_case_agent` 文件夹（必须包含 `assets/`），配置本机NX路径和许可，然后运行：

```powershell
$env:NX_AGENT_NXBIN='D:\UG12.0\UG\NXBIN'
# 本机SPLM_LICENSE_SERVER按已有NX许可配置
python -X utf8 nx_case_agent/agent.py web --port 8771
```

打开 http://127.0.0.1:8771 ，输入描述并核对参数，再执行建模。每次输出独立任务文件夹和NX12 PRT，执行结果列出路径。也可双击文件夹中的start.cmd。

不用提供以前的对话或再寻找桌面源文件；完整几何已随智能体的assets目录移交。服务执行前以及NX载入前均校验资产SHA256，资产缺失或损坏会阻止建模。这里的“学习”是固化案例几何、参数关系和程序，没有训练LLM权重。

## 两个案例与参数范围

| 案例 | 几何生成方式 | 真正可编辑的参数 |
|---|---|---|
| 二维轮盘截面 | 原始STEP完整非均匀B样条的控制点和节点，关联点/直线/样条→平面面体；1个面、14条边 | X1～X5、Y1～Y4，以及内侧位置；分段映射驱动完整控制网 |
| 三维单叶片轮盘 | 内置原始new160.x_t完整B-rep→NX原生非均匀缩放；1个实体、44个面、122条边 | hub_length和rim_radius，分别控制轴向和径向整体尺度 |

三维内孔半径、叶尖半径、轮毂大台阶半径、外缘宽度、薄盘局部厚度和叶高为关联尺寸。改变径向尺度会同时改变孔径、盘缘和叶高；改变轴向尺度会同时改变盘缘宽度和各轴向厚度。不能把这些尺寸误认为可独立优化。没有恢复的局部参数若被要求单独改变，规划会报错，保留原始曲面。

自然语言支持Markdown加粗数值、“约为”“外缘轴向宽度”“叶尖到轮盘轴线的距离”等表述。旧描述中的71.7毫米是参考半径71.68毫米的标称值；二维的20.81等旧值同样经过四舍五入。匹配已知标称尺寸时使用参考精确值，并在nominal_resolutions中记录。结构化parameters是明确修改入口，不做标称值吸附。

例如：

> 请精确建立带一片叶片的三维轮盘局部，保留原始轮毂台阶、真实叶片曲面与叶根过渡。

> 请精确建立二维轮盘截面，保留原始样条和九个设计参数。

```python
from nx_case_agent import plan, execute
preview = plan('精确建立三维单叶片轮盘')
# 修改轮毂长度会联动全部轴向尺寸，界面及结果显示关联关系
result = execute('精确建立三维单叶片轮盘',
                 parameters={'hub_length':58.0},
                 jobs_root='output/nx_cases')
```

## 精度验收

每次实际生成均执行：

1. 检查实体/面体类型及完整拓扑：二维1面14边；三维44面122边。
2. NX几何内核检查：body consistency、face-face intersection、逐面self intersection。
3. 对每个真实独立参数扰动0.05，检查几何变化、关联尺寸、内核有效性，再恢复。
4. 保存、关闭并重开PRT，重新测量并检查几何。
5. 与独立参考几何进行双向边界及裁剪面内采样距离比较，同时核对面积和体积。距离阈值0.001毫米，面积/体积差阈值0.001 mm²/mm³。

二维参考用原始节点和控制点经UF直接构造，候选使用NX关联控制点和StudioSpline；三维基线参考独立载入原始Parasolid，候选带缩放特征。变更尺寸时，比较目标是按同一明确变换形成的参考，不宣称仍与未修改的零件完全相同。

偏差结果是采样验证，不是连续曲面全域Hausdorff上界证明。结果保留采样数、覆盖面数、双向最大距离、面积/体积差和来源哈希，不能只凭体积或面数一致判定几何一致。源文件原有微小不对称和接缝按原样保留，不人为镜像或磨平。

## DiskAgent与MCP

DiskAgent重启后访问 `/case-modeling`，或从自动建模页面进入“两案例建模智能体”。路由仍为 `POST /api/nx/cases/plan` 和 `POST /api/nx/cases/build`。

独立MCP可选安装依赖：`python -m pip install mcp`。

```json
{
  "mcpServers": {
    "nx12_cases": {
      "command": "<Python绝对路径>",
      "args": ["<nx_case_agent绝对路径>/mcp_server.py"],
      "env": {
        "NX_AGENT_NXBIN": "<本机NX12目录>/NXBIN",
        "SPLM_LICENSE_SERVER": "<本机许可设置>"
      }
    }
  }
}
```

工具：list_learned_models、preview_model、build_model。DiskAgent原NX MCP服务中的nx_case_preview和nx_case_build也调用本模块。

## 开发与证据

模块职责：precision_recipes.py定义精确案例、真实参数和资产哈希；core.py负责解析与规划；exact_geometry.py创建完整几何；nx_kernel.py负责内核和双向偏差检查；native_journal.py执行NX生成与验收。recipes.py仅为兼容导出。

运行规划测试：`python -m unittest nx_case_agent.test_core`。
真实MCP→NX验收：`python nx_case_agent/acceptance.py`；加 `--rounds 2` 可重复两轮。只提交regression_prompts.json中的原始description，不额外补传参数。最新验收结果在acceptance_mcp.json，示例PRT在examples目录。
