# EIE575 本地仿真 MCP

五个工具控制现有 OMNeT++ 5.6.2 / Veins 5.0 / SUMO 1.6.0 工程。复用原工程 `manage.py` 的运行流程和 `analyze.py` 的验收标准；新增的部分只负责任务队列、进程归属、状态和 MCP 协议。

当前支持 S1 两车、30 s、G0–G3 与指定随机种子。S0/S2、背景车流、正式实验矩阵、单次仿真暂停和断点恢复不在此版本内。仿真算法、无线参数和步进控制均沿用现有工程。

## 安装并接入

本机现有解释器 `E:\港理课程\.venvs\courseware-mcp\Scripts\python.exe` 已有依赖，无需升级仿真软件。

在你有写入权限的普通 PowerShell 中执行解压目录的 `install.ps1`。本次本机工作副本可直接执行：

```powershell
& 'E:\Temp\eie575-simulation-mcp\install.ps1'
```

安装器把程序放到 `E:\港理课程\课程文件\eie575\project\simulation_mcp`，把运行数据放到 `E:\EIE575_Tools\workspace\eie575_mcp_runs`，备份并更新课程项目 `.codex\config.toml` 中本服务的配置，然后启动隐藏的独立宿主。不会修改其他 MCP 服务或目录权限。若同名服务来自另一安装，或目标非空且不属于本安装器，安装器会停止。

安装完成后，重载 MCP 连接或重新打开课程项目，工具服务名为 `eie575-simulation`。这一步才代表工具已加入聊天的工具列表。本次开发环境的目标目录与注册配置拒绝写入，因此没有在本轮自动执行正式安装或注册；已经通过标准 MCP 客户端完成真实协议验收。

可覆盖安装路径：

```powershell
.\install.ps1 -InstallDir 'D:\eie575-mcp' -StateRoot 'D:\eie575-runs' -ConfigFile 'D:\course\.codex\config.toml'
```

程序本身可放中文目录；`state_root` 与 `tools_root` 必须用不含空格的英文路径。原仿真安装从 `settings.json` 的 `source_project` 与 `tools_root` 读取。修改源工程后，下次任务会复制新的输入与 exe；需由原工程流程保证 exe 已按相应源码编译。

## 五个工具

| 工具 | 参数示例 | 返回与用途 |
|---|---|---|
| `start_run` | `{"schemes":["G0","G3"],"seeds":[0],"request_id":"trial-01"}` | 返回 job_id；方案×种子串行运行，最多100次；相同 request_id 和参数重试返回同一任务。 |
| `get_status` | `{}` 或 `{"job_id":"..."}` | 环境、宿主心跳、任务状态、仿真时间、进程身份和完成次数。 |
| `tail_log` | `{"job_id":"...","log":"simulation","lines":80}` | 已落盘日志末尾；类别为 simulation/launcher/launchd/worker/driver；最多500行、64KiB。 |
| `stop_run` | `{"job_id":"..."}` | 中止本任务当前运行及后续队列，清理其所属进程；保留不完整结果，不改变已经完成的任务。 |
| `get_results` | `{"job_id":"..."}` 或再加 `"run_index":0` | 默认返回批次精简验收摘要；指定从0开始的运行序号返回完整 summary 和文件清单。 |

推荐先查 `get_status`，确认 `environment.ready` 和 `host.ready` 都为 true，再启动任务。完成后必须确认 `get_results.complete=true`；只有文件存在或进程退出不足以说明实验通过。暂停/恢复不是 `stop_run` 的含义。

## 后台运行与监控

独立宿主从普通 PowerShell 启动，MCP 服务只提交文件队列。Windows MCP 客户端会清理自身进程树，所以宿主不从 MCP 服务内部启动。已实测断开 MCP 再连接，任务继续运行。

机器重启后，在安装目录执行 `start_host.ps1`；重复启动只会复用同一运行目录的既有宿主。此版本未设置开机自启。启动请求立即返回，宿主和 worker 继续执行；任务记录保存在运行目录中。

当前同时最多一个活动批次，避免覆盖与串扰。每次使用独立英文目录复制必要工程文件并记录 SHA-256；SUMO 启动器端口由原脚本选择。worker 异常退出时 Windows Job Object 自动清理其仿真后代；停止不按进程名批量杀进程。

状态每秒持久化，仿真进度来自约每5秒现实时间刷新的原引擎日志。小规模30秒仿真可能在不足1秒的实际计算时间内结束，因此只看到起点和终点是正常的。CBR/TX/RX CSV 可能缓冲，不承诺实时曲线；这些指标以结束后验收为准。心跳与最后日志时间分别返回，没有新日志不直接等同卡死。

## 命令行也通过 MCP

```powershell
$py = 'E:\港理课程\.venvs\courseware-mcp\Scripts\python.exe'
& $py -B -X utf8 .\client.py --list
& $py -B -X utf8 .\client.py get_status --args '{}'
& $py -B -X utf8 .\client.py start_run --args '{"schemes":["G0"],"seeds":[0],"request_id":"demo-01"}'
& $py -B -X utf8 .\client.py get_results --args '{"job_id":"填入返回的任务编号"}'
```

`client.py` 使用官方 `ClientSession` 和 stdio，不直接调用控制器函数。即使聊天尚未加载本服务，也能用该入口验证协议。JSON 参数需按所用终端的规则传递；Python API 示例见 `tests/smoke_mcp.py`。

## 验证

```powershell
& $py -B -X utf8 -m unittest discover -s tests -p 'test_*.py' -v
& $py -B -X utf8 .\tests\smoke_mcp.py
```

第二条会真实运行 G0/G3 并启动一轮取消测试，需要宿主已启动且无活动任务。验收证据见包内 `evidence/mcp_smoke.json` 与 `docs/验收记录.md`。安装器仅复制代码、配置和文档，证据保留在交付包中。

## 接续入口

- `PROJECT_INDEX.md`：文件和命令入口。
- `PROGRESS.md`：验证状态与剩余注册步骤。
- `docs/施工手册.md`：接口、边界和实现理由。
- `settings.json`：源工程、工具安装和运行数据路径。
- `requirements.txt`：本次验证依赖版本。

官方参考：[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)、[Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)、[Veins TraCI](https://veins.car2x.org/documentation/modules/)、[SUMO TraCI](https://sumo.dlr.de/userdoc/TraCI/)。
