# Git版本管理说明

项目目录：`D:\DSV_20261022_1`。初始分支：`main`。

## 本次纳入的内容

- `agent_app` 源码、HTML工作台、示例任务与隔离测试。
- `python_control` 原控制器、采样、GP、Workbench调用和搜索配置。
- `api_bridge` 接口代码、九参数定义及不含实际密钥的API配置。
- CAD `.prt`、Workbench `.wbpj` 及配套几何、Mechanical和材料数据库。
- 启动脚本、原生求解模板与说明文档。

CAD与Workbench工程源数据合计约12MB，直接纳入本地Git；会话journal、设计点日志和缓存不作为工程源数据提交。

## 保留在本机但不入库的内容

- `llm_runs`、`python_llm_runs`：真实运行历史、求解产物和断点。
- `agent_workflows`、`agent_plans`、`agent_app_logs`：工作流、规划及服务运行状态。
- `python_control/optimization_config.json`：包含本机Workbench位置的运行设置。
- 编辑器个人设置、环境文件、密钥、日志、锁、缓存及临时文件。

忽略规则不会删除已有文件。历史结果需要单独备份；只复制Git仓库并不能恢复所有历史计算。

## 换行与断点指纹

程序按文件原始字节计算SHA256，部分文件保留不同换行形式。因此 `.gitattributes` 使用 `* -text`，仓库本地配置使用 `core.autocrlf=false`，不自动重写换行。文本diff仍然可用。

切换版本、恢复文件或改变配置时，应先确认没有正在进行的优化。旧断点要求原工程定义、代码、配置和路径兼容；不要对运行中的工程直接执行版本恢复。

## 日常保存版本

```powershell
Set-Location -LiteralPath 'D:\DSV_20261022_1'
git status
git diff
git add -- agent_app README_性能预测智能体.md
git diff --cached --stat
git commit -m "Describe the completed change"
```

`git add` 后的文件是暂存区，`git commit` 才形成一个历史版本。按本次修改选择路径后提交，便于说明每个版本的实际变化。

## 查看历史

```powershell
git log --oneline --decorate -10
git show --stat HEAD
git diff HEAD
```

本次只建立本地版本库，不配置远程地址或上传。后续如需GitHub同步，再指定目标仓库。

## 新目录或新电脑

首次检出时，从 `python_control/optimization_config.example.json` 复制出本机 `optimization_config.json`，设置真实Workbench路径。准备兼容的Python依赖、ANSYS与许可证、用户API密钥环境变量，并核对工程中的外部引用。工作台启动器使用当前电脑Python绝对路径，换电脑需相应修改。

代码/工程文件都已入库，但软件环境、授权、密钥和历史运行数据不在Git中。新目录应建立新的兼容运行；旧断点的恢复仍须通过原有路径和指纹核验。
