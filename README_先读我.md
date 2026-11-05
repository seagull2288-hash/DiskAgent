# 二维轮盘优化：当前可运行版本

当前入口是 `run_python_optimization.py`，使用 Python 控制 DeepSeek、搜索中心采样、代理筛选和 Workbench 求解。无需 MATLAB。

## 1. 先检查

在 PowerShell 中运行：

```powershell
& "$env:USERPROFILE\.conda\envs\llm_opt\python.exe" -u -B 'D:\DSV_20261022_1\run_python_optimization.py' --check
```

检查只确认本机配置和依赖，不请求 API，也不启动 Workbench。环境变量来源应显示 `Deepseek_KEYS1`，`api_key_configured` 应为 `true`。

## 2. 开始新的100次优化

以下命令会实际调用 API 并启动有限元求解，请在需要开始计算时执行：

```powershell
& "$env:USERPROFILE\.conda\envs\llm_opt\python.exe" -u -B 'D:\DSV_20261022_1\run_python_optimization.py' --live --rounds 100
```

也可以使用 PowerShell 启动器：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'D:\DSV_20261022_1\start_python_optimization.ps1' -CandidateCount 100
```

100指新增候选的真实评价预算，另外还有1次参考设计计算；不等于100次 API 请求。搜索和筛选规则详见 `README_搜索中心与代理筛选.md`。

`python_control\optimization_config.json` 中的 `candidate_count` 原值未改；上述命令的 `--rounds 100` 或 `-CandidateCount 100` 会为这次运行指定100次预算。

## 3. 配置在哪里

| 位置 | 用途 |
|---|---|
| `python_control\optimization_config.json` | 工程、预算、输出目录、Key来源变量名 |
| `python_control\search_config.json` | 搜索中心、采样、批次和代理筛选配置 |
| `api_bridge\config.deepseek.json` | API地址、模型名称与内部认证变量名 |
| `api_bridge\problem.json` | 九个变量、范围、参考尺寸和参数映射 |
| `project_v2` | Workbench工程及其配套数据，须作为整体保留 |
| `cad` | 工程使用的CAD原件，宏要求恰有一个 `.prt` |

Windows用户环境变量名为 `Deepseek_KEYS1`。程序读取它后临时提供给接口变量 `DEEPSEEK_API_KEY`；后者不用改名。不要把实际Key写进源代码或JSON。

## 4. 新结果在哪里

新运行会自动建立 `python_llm_runs\run_...`，记录每轮尺寸、真实应力、候选来源和断点。完成后生成收敛图；根目录 `python_optimization.log` 会显示当前进度。

同一版本的新Python运行可使用原运行目录续跑：

```powershell
& "$env:USERPROFILE\.conda\envs\llm_opt\python.exe" -u -B 'D:\DSV_20261022_1\run_python_optimization.py' --live --rounds 100 --resume 'D:\DSV_20261022_1\python_llm_runs\run_实际目录名'
```

不要修改工程、代码或搜索配置后直接复用旧断点。Workbench工程正在被其他窗口或任务使用时，程序会拒绝计算；不要直接删除工程锁。

## 5. 已保留的100次历史结果

唯一保留的旧运行在原位置：

`llm_runs\run_20261023_230731_tp2e560e6a_22b2_4bc1_9833_5ebc630c6499`

- `history.csv`：1次基准和100次候选的真实计算历史。
- `best_design.json`：最佳尺寸与应力。
- `plots`：已保存的结果图。
- `iterations`、`evaluations`、`messages`：每次求解、API交互和核验记录。
- `checkpoint.mat`：原MATLAB运行的断点归档。

这份运行已完成100/100，最佳应力约612.7034485 MPa。清理方案完整保留其原有结果目录并移除旧MATLAB执行代码；该 `.mat` 作为历史归档，不能交给当前Python入口续跑。新的Python运行需要独立建立基准和历史。
