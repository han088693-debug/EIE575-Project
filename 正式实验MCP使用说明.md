# EIE575 TABR 主线 MCP 使用说明

适用版本为 2026 年 11 月 23 日。日常查看 E0 校准、工程探针或已登记批次，使用 `eie575-tabr-formal`。早期 S1 两车 30 秒功能实验使用 `eie575-simulation`。两个服务都有 `start_run`，参数和结果编号不同。

## 1　连接已有工程

原电脑的主工程是 `E:\Temp\eie575_tabr`，正式服务入口是工程下的 `tools\formal_mcp.py`。已有这份工程时，直接在 PowerShell 读取它的配置。将这几行保留在同一个窗口，供下文复用。

```powershell
$project = 'E:\Temp\eie575_tabr'
Set-Location -LiteralPath $project
$cfg = Get-Content -Raw -Encoding UTF8 .\configs\core.json | ConvertFrom-Json
$mcpPy = $cfg.tools.mcp_python
$analysisPy = $cfg.tools.analysis_python
$client = Join-Path $cfg.tools.mcp_dir 'client.py'
$server = Join-Path $project 'tools\formal_mcp.py'
```

| 组件 | 用途和位置 |
|---|---|
| 本包 `TABR_project\` | 文件按主工程相对路径排列。另机使用时，先从 575 实验完整包按项目说明恢复匹配的主工程，再按需使用同版 MCP 文件。已经安装本版的电脑可直接连接原工程。 |
| 本包 `simulation_mcp\` | 包含主线共用的 `client.py`、`controller.py`、`process_control.py` 等。主配置 `tools.mcp_dir` 要指向实际包含这些文件的目录。单独使用主线时，共用模块可直接从该目录导入。 |
| `configs\core.json` 的 `tools` | 指定 MCP Python、分析 Python、共享模块、工具链和启动器路径。另机使用时根据实际路径配置工作副本。保留冻结配置、历史回执及原始数据，历史结果沿用其对应版本。 |
| Python 与仿真软件 | Windows 上使用已装 `mcp`、`psutil` 的 MCP Python，依赖版本见 `simulation_mcp\requirements.txt`。分析 Python 需能运行原工程的 `experiment.py`（配置验证用 `jsonschema`）。真实仿真另需项目的 EXE、场景、OMNeT++ 5.6.2、Veins 5.0、SUMO 1.6.0 和原 Veins 启动器使用的 Python 2。 |

## 2　查看服务和预演矩阵

下列命令依次列出 MCP 工具、读取宿主状态、列出已登记探针、预览候选配置的实验矩阵。预演只输出计划。

```powershell
& $mcpPy -B -X utf8 $client --server $server --list
& $mcpPy -B -X utf8 .\tools\probe_mcp.py status
& $mcpPy -B -X utf8 .\tools\probe_mcp.py list
& $analysisPy -B -X utf8 .\tools\experiment.py plan --config .\configs\core.json --scope formal --dry-run
```

工具列表应有下表六项。`status` 返回 `host.ready`、队列路径、批次根目录及最近请求。若 `host.ready` 为 `false`，从普通 PowerShell 执行下面一条命令启动独立后台宿主，再运行 `status`。默认队列路径按工程的绝对路径生成。若设置了 `EIE575_FORMAL_QUEUE` 或 `EIE575_FORMAL_BATCH_ROOT`，宿主与客户端须使用相同设置。

```powershell
& $mcpPy -B -X utf8 $server --start-host
```

要在 Codex 中调用，已有相应配置时直接打开课程项目。需要手动接入时，可在该项目的 `.codex\config.toml` 增加以下 stdio 服务并重新加载连接。示例采用原电脑路径，另机用实际路径替换 `command` 和 `args` 最后一项。宿主应从上面的 PowerShell 入口启动。

```toml
[mcp_servers.eie575-tabr-formal]
command = 'E:\港理课程\.venvs\courseware-mcp\Scripts\python.exe'
args = ['-B', '-X', 'utf8', 'E:\Temp\eie575_tabr\tools\formal_mcp.py']
```

当前候选配置预演应返回 `dry_run=true`、`formal_jobs=100`、`paired_blocks=25`、`duplicate_jobs=0`。这些数字表示计划规模。真正登记正式批次还需冻结配置及对应 P3 凭据。

## 3　运行、查看和取回结果

| 工具 | 必要参数和用法 |
|---|---|
| `start_probe` | `probe` 取自 `configs/formal_probes.json`，`request_id` 由操作者指定。提交一个工程或先导探针。 |
| `start_run` | `batch` 为已登记的批次目录名，`job` 为该批次 `batch.json` 中的作业编号，另传 `request_id`。可选 `resume` 默认 `false`。每次调用提交一项。 |
| `get_status` | 不传参数查看宿主和最近请求，传 `request_id` 查看该请求。 |
| `tail_log` | 传 `request_id`，`log` 可选 `driver`、`simulation`、`launcher`、`launchd`、`engine`、`wall_budget`。行数 `lines` 为 1–500。 |
| `get_results` | 传 `request_id`，取回实际 attempt、作业指纹和指标。 |
| `stop_run` | 传 `request_id` 请求取消。继续查询状态，直至出现终态。 |

**实际运行探针。** 确认要启动计算后，从 `list` 结果选择探针；以下为项目已登记的一个例子。每个新请求使用新的编号。同一编号和同一参数重复提交会返回已有记录。当前队列同时接受一个活动请求。

```powershell
$request = 'demo_' + (Get-Date -Format yyyyMMdd_HHmmss)
& $mcpPy -B -X utf8 .\tools\probe_mcp.py run p2_resource_16_G0 --request-id $request
```

**断开后接续。** 重新打开 PowerShell 时，先执行第 1 节，恢复 `$request` 为原编号。后台宿主正常运行时，断开客户端后作业会继续。

```powershell
& $mcpPy -B -X utf8 .\tools\probe_mcp.py status $request
& $mcpPy -B -X utf8 .\tools\probe_mcp.py wait $request
```

`wait` 在终态将回执写到 `verification\probe_receipts\<request_id>.json`。验收时查看 `status.state == "completed"` 和 `result.complete == true`，再按 `result.attempt` 定位此次运行，按 `scope` 分清工程、先导或正式样本。若为 `failed`、`cancelled` 或 `interrupted`，查看 `driver` 及可用的 `engine`、`simulation`、`wall_budget` 日志。只想重新查看同一作业时使用 `status` 或 `wait`。
