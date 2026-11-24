# β 扫描归档（2026-11-24）

## 内容

| 文件 | 说明 |
| --- | --- |
| `summary.csv` | 6 条运行的汇总，两个速率窗口并列（事件窗 / 制动窗），带 `b_min_hz`、`w_max` |
| `SHA256.txt` | 入库配置、汇总表、以及原始 attempt 快照的哈希 |

入库配置在 `TABR_project/configs/probes/`：

| 配置 | 部署模式 | β | b_min_hz | cbr_target |
| --- | --- | ---: | ---: | ---: |
| `bsweep_v4_b0p5.json` | v4（背景定速 10 Hz） | 0.5 | 0.6667 | 0.42 |
| `bsweep_v4_b1p0.json` | v4 | 1.0 | 0.5000 | 0.42 |
| `bsweep_v4_b1p5.json` | v4 | 1.5 | 0.4000 | 0.42 |
| `bsweep_v4_b2p0.json` | v4 | 2.0 | 0.3333 | 0.42 |
| `bceil_s1_b2p0.json` | all_vehicles_same_scheme | 2.0 | 0.3333 | 0.18 |
| `bceil_s1_b3p0.json` | all_vehicles_same_scheme | 3.0 | 0.2500 | 0.18 |

## 来源与等价性

六份配置不是事后重写的，而是从各 attempt 目录的 `config_snapshot.json` 原样取出、只重排了 JSON 缩进：

- 语义上与原快照逐字段相等（已用 `json` 对象比较核对）；
- 字节哈希不同（缩进与行尾不同），因此 `SHA256.txt` 同时记录入库配置和原快照两组哈希。

原始 attempt（含日志、metrics、快照）在 `E:\Temp\bsweep`，按工作流约定不入库；本目录只保留可由配置重建的汇总结果。

## 口径

`summary.csv` 的两个窗口不可混用：

- `lead_*`：事件窗 `[t0, t0+post_event_s]`，compact 配置下为 10 s；
- `lead_brake_*`：同一事件窗内 `B=1` 的时隙，即"制动窗"，`B=1` 约占事件窗 37.6%。

"制动窗平均 r" 与 "触顶比例" 只看 `lead_brake_*`；"事件窗平均 W" 只看 `lead_W`。报告 §5.8 与 `TABR_project/docs/BSWEEP_20261124.md` 使用同一组口径。

## 复现校验（2026-11-24）

入库的 β=2 配置重跑过一次，用来证明"入库配置 = 当时跑的配置"：

- 运行：`configs/probes/bsweep_v4_b2p0.json`，S1 / high / G2 / seed 300，输出到 `E:/Temp/bsweep_repro`；
- 结果：attempt `20261124T092632159145Z-b96b58c7159b`，`data_validity = valid`；
- 与归档行逐字段比对 29 个可比字段，**全部相同**，包括 `all_tx = 122,363`、`all_rx = 10,036,398`、制动窗 r = 6.93 Hz、事件起点 D_info = 100.5 ms；
- 该次运行的 `metrics.json` SHA256：`b58db29602bc84afa22cc985724eaa386947596d8ccea8e1c6df79daf5893284`。

## 复现

```powershell
python -B -X utf8 tools/experiment.py probe --config configs/probes/bsweep_v4_b2p0.json `
  --output E:/Temp/bsweep/b2p0_s300_G2 --scenario S1 --load high --scheme G2 --seed 300
python tools/bsweep.py --root E:/Temp/bsweep --out E:/Temp/bsweep/summary.csv
```

## 边界

- 每档 n=1（单种子、单事件），只用于确认速率律与上限，不作统计结论；
- compact 场景（3000 m / 32 s）与主实验（8000 m / 120 s）工作点偏差在 3% 以内，绝对值不与主实验并列；
- 结论限于 v4 部署、d=33、80 km/h、250 B、单信道、单一 MAC 优先级。
