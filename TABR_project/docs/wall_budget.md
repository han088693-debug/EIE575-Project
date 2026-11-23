# 运行预算

每个真实作业沿用配置中已登记的 `execution.wall_timeout_s`。缺省值为 1800 秒。P4 的已冻结分层预算仍作为初始下限。经单独批准的请求可以采用更高硬上限。批准单只适用于其中列明的请求。已加载旧运行器的进程继续使用它启动时读到的值。

## 启动前

由有授权的本地操作者保存批准记录及其来源，取得原始文件字节的 SHA-256。记录格式为 `eie575-wall-approval-v1`，包含 `approved=true` 和 `requests` 数组。每项包含以下字段。

| 字段 | 内容 |
|---|---|
| `request_id`, `scope`, `job_id` | 预定的 MCP 请求 ID、登记范围和科学作业 ID |
| `registration_sha256` | 工程探针取 MCP 对已登记规范和配置原文的组合哈希。正式作业取冻结批次的 `physics_sha256` |
| `physics_sha256` | 正式批次取同名字段。探针将登记的密度覆盖到验证通过的配置，再调用 `experiment.freeze_physics_sha256`，与 `experiment.probe` 口径相同 |
| `authorized_cap_s` | 该请求单独批准的绝对墙钟上限，单位为秒 |
| `initial_mode` | `full_cap` 直接用批准值。E0 的 `evidence` 模式从已验证的历史测量估算 |
| `continue_on_progress` | 布尔值。为真且进度证据满足条件时，同一进程可将较小初始预算提高到该请求原有硬上限 |
| `initial_limit_s` | 可选。明确选择分阶段生效的初值，须在硬上限以内，并满足已有分层下限与统计建议 |
| `benchmark_sha256`, `benchmark_context` | `evidence` 模式必填。上下文含 `density`, `duration_s`, `slots`, `machine_id`, `cost_group`, `fallback_s` |

使用本工程配置的分析 Python，在工程根目录运行下列 PowerShell。变量分别来自已批准记录、冻结的基准和计划中的请求。单纯生成示例 JSON 无法代替实际授权。哈希用来核对文件完整性，操作者负责核验其来源。

```powershell
& $python tools/wall_budget.py register --request $request --approval $approval --sha256 $approvalSha
```

采用历史估算时追加 `--benchmarks configs/e0_serial_benchmarks.json --benchmark-sha256 $benchmarkSha`。当前封存的 E0 串行样本包 SHA-256 为 `5f95f84e1bb041ce78f4e5e2a7633180303ab4250f9e2a3a959c82780321df20`，使用时须保留其引用的历史清单、测量、配置和引擎状态。估值超过获准上限时，启动回执记录未裁估值、基准 attempt、硬上限和原因，并采用已批准时长。尚无可比基准时，记录未测规模并采用该请求已批准的上限。

通过原 MCP `start_probe` 或 `start_run` 提交同一请求 ID。运行器核对科学输入和请求范围，绑定唯一 attempt、run ID、作业指纹。将 `verification/wall_runtime/<request_id>` 与 attempt 的 `logs` 一起归档。

## 运行中

MCP `tail_log(request_id, log="wall_budget")` 返回预算事件。每 1800 秒做一次软检查。距硬上限只剩 15% 或 300 秒时发出提前预警，使用两者中较短值。检查固定 run 的两份 CSV 最后一条完整记录，与所属进程树 CPU 共同判断。两份时钟均前进可证明仿真推进。CPU 活动而时钟静止标为可能存在缓冲。错误的时钟或证据缺失单独记录。达到硬上限后终止本次所属进程。

初始预算低于已批准硬顶时，可在初始预算到期前提交单调增加的时限。单位为自该 attempt 启动起累计可用的总秒数。

```powershell
& $python tools/wall_budget.py extend --request $request --attempt $attemptId --job-key $simulationJobKey --seconds $newTotalSeconds --reason $reason
```

命令返回 `published=true` 表示已写好指令。同一运行器消费后，审计中出现带 revision 和指令哈希的 `operator_extension`。过期、重复、他项作业、关闭进程、超硬顶或缩短时限的更新会被拒绝。进程死亡后，新 attempt 需要新的请求和相应授权。

## 核验与恢复

核验 `logs/wall_budget.jsonl`、`logs/wall_budget_state.json`、`logs/engine_terminal.json` 和 `logs/engine_status.json`。引擎终态先于 Cmdenv 原始日志移动保存。Windows 文件共享冲突时可保留原件并核对副本字节；其他归档错误写入 `finalization_error`。使用完整原始日志核验 `evidence_pending` 的 attempt，再决定后续处置。部署该运行器会改变已签名的引擎源码指纹，应封存旧运行器并在新版本中启用。
