# EIE575 主线 E0 校准检查点

本次 E0 独立复核了 12 个有效唯一运行，另外保留 6 次工程失败。判定为 **预注册候选网格已用尽**（`grid_exhausted`）。具体判据、候选密度与数值见本目录 `E0_校准结论.md` 和 `candidate_decisions.csv`。本阶段归档的正式矩阵运行数为 0。

`target_windows.csv`、`spatial_windows.csv` 和 `traffic.csv` 分别给出目标车辆窗口、空间分组及流量指标。`verification.json` 是原始数据复核结论。`EIE575_E0_恢复验收.json` 是课程目录现存文件的空目录还原与独立复算回执。

## 离线恢复一个完整主线项目

请保留现在这两个版本目录之间的相对位置。`../2026-11-22_补充实验归档/EIE575_实验完整归档.zip` 提供 P2、P3 启动及首个 E0 检查点，`EIE575_E0_差异.zip` 提供新增原始数据、历史错误与运行回执、配置、版本、索引和诊断。`EIE575_E0_SHA256.json` 列出父包、增量及本阶段直接可读文件的外部 SHA-256。

在本目录打开 PowerShell，用 Python 3.11 以上执行。目标选短英文无空格的新目录。恢复只需要标准库及这两个本地版本，不访问原工作树，不启动仿真。

```powershell
python -B -X utf8 restore_e0.py --destination E:\Temp\e0verified
```

还原工程中的 `archive_e0/archive_index.json` 提供每个请求的 `attempt_relative`。据此可找到原始 `inputs/`、`logs/`、`metrics.json` 和 `run_manifest.json`。`archive_e0/mcp_receipts/` 保存终态请求证据。`results/calibration/core_v1_budget2/` 的 `index.json`、`decision.json`、`measurements/` 保留原件，绝对路径字段记录历史执行位置。可用以下命令从新树独立复算校准表与 `density_cbr.png`，绘图使用 Pillow 和 Windows 等线字体。

```powershell
python -B -X utf8 E:\Temp\e0verified\archive_e0\diagnostics\review_e0.py --project E:\Temp\e0verified --campaign core_v1_budget2 --out E:\Temp\e0_recalc
```

## 预算修订与历史边界

`results/calibration/core_v1/` 保留首版 16 个请求、10 次有效的工程阻塞现场。修订预算采用两个独立 MCP 槽，每次最长 5400 秒；两份新增请求和回执对应密度 25、seed 100/101。最终选择器从 `core_v1_budget2` 读取完整 18 项历史。双槽 960 辆工程复演位于 `archive_e0/engineering/p2_960_parallel/`，来源与原P2基准匹配关系见 `concurrency_index.json`；该复演不计入E0或正式矩阵。

本目录的 `restore_e0.py` 是通过 Win32 长路径回归的交付入口。差异 ZIP 连同采集时的封存脚本保持原始字节。
