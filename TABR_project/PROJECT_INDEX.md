# EIE575 TABR 项目入口

- 决定与门槛见 docs/EIE575_Simulation_Execution_Plan.md。当前状态见 PROGRESS.md。P2 交通与测量实现约定见 docs/P2_场景与观测接口.md。
- 旧版 S1 基线使用 run.ps1 与 tests/run_core_tests.ps1。P0 下载恢复的回执为 verification/p0/restore_receipt.json。
- 正式 CLI 使用 experiment.ps1。候选配置为 configs/core.json，正式数据需有已冻结的 P3 校准证据。开发用的注册表由 tools/register_p2.py 生成。
- 正式 MCP 使用 tools/formal_mcp.py 和 tools/probe_mcp.py；子命令 list、run、status、wait、stop。每次请求和对应 run 的回执位于 verification/probe_receipts。
- P1 合成验收见 verification/p1/。P2 独立交通结果见 verification/p2/sumo/index.json，联合仿真见 results/probes/；tools/p2_evidence.py 复算验收，tools/resource_probe.py 检查资源和同种子等价性。
- tools/checkpoint.py pack、restore 和 verify 用于带 SHA-256 的工程检查点。

- P2 真实放行凭据位于 verification/p2/acceptance.json。P3 E0 的活动清单位于 results/calibration/core_v1/index.json，校准判定在同目录的 decision.json。实际项目根目录为 E:\Temp\eie575_tabr。

- 稀疏跨车道补充实验（独立 E0，密度 1/2，16 次实测）结果在 E:\Temp\e575x\results\sparse\analysis，其同种子配对与原始登记见补充工程 PROJECT_INDEX.md。
