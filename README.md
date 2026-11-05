# DiskAgent：二维轮盘优化工作台

面向当前九参数二维轮盘的本地应用：自然语言任务规划、有状态批次调度、LLM搜索中心、Python候选采样与GP筛选、Workbench有限元评价、批次反馈、性能预测影子审计与结果展示。

当前目标为最大等效应力最小化。强度许用值、位移上限、质量及寿命等工程判定，以具体配置和报告中的已实现范围为准。

## 文档入口

- [工作台与调度流程](README_轮盘智能体工作台.md)
- [批次反馈与决策追踪](README_批次反馈闭环.md)
- [性能预测智能体与影子验证](README_性能预测智能体.md)
- [搜索中心、采样与代理筛选](README_搜索中心与代理筛选.md)
- [自然语言规划器](README_自然语言规划器.md)
- [结构化任务接口](README_智能体任务接口.md)
- [原Python优化入口与本机配置](README_先读我.md)
- [Git版本管理与恢复注意事项](README_Git版本管理.md)

## 主要目录

| 目录/文件 | 内容 |
|---|---|
| `agent_app/` | 工作台、规划/调度/评价、反馈、预测审计与隔离测试 |
| `python_control/` | 原优化控制器、采样、GP与Workbench调用 |
| `api_bridge/` | 模型接口、非密钥API设置、九参数问题定义 |
| `cad/` | 当前CAD模型源文件 |
| `project_v2/` | 当前Workbench工程和配套几何/Mechanical/材料数据库 |
| `mechanical_template.py.txt`、`make_verified_journal.py` | 原生求解与核验模板 |

## 本机运行

已有环境可双击 `start_disk_agent.cmd`，工作台地址为 http://127.0.0.1:8765/ 。启动器中的Python路径使用当前电脑环境；换电脑需先调整该路径。

首次检出到新目录时，复制 `python_control/optimization_config.example.json` 为 `python_control/optimization_config.json`，填写本机真实Workbench可执行文件位置，并准备Python环境、NumPy、Matplotlib、ANSYS 2020 R2及许可证。实际API密钥放入该配置指定的用户环境变量。

预览默认不启动优化LLM或Workbench；若启用规划LLM，规划本身会使用API。真实优化通过工作台的执行按钮或CLI的 `--live` 明确启动。

## 版本与数据边界

Git管理源码、测试、文档、非密钥API/问题/搜索配置、CAD与Workbench工程源数据。运行历史、工作流状态、预测审计、求解结果和日志保留在本机原目录，不加入Git；Git仓库不替代结果备份。

当前工程文件是首次提交时的工作区快照。复制到新目录或更换机器后，需重新核对工程引用和本机配置。旧运行/工作流还绑定绝对路径和字节指纹，不能仅凭检出代码就直接跨目录恢复旧断点。
