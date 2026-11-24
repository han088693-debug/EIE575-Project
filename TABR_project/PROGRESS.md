# TABR 仿真实施进度

- P0 已验收。首次 run 和取回后的干净恢复 run 分别通过四组 seed 0。完整回执见 verification/p0/restore_receipt.json。
- P1 已接通候选配置、正式批次入口、配对矩阵、断线/重试和正式数据分析。用 synthetic 验证有效漏收、缺日志、中断与改版等边界，仅在 P2 接入真实引擎后报告真实工程样本。verification/p1/ 保存合成测试和 MCP 协议验收结果。
- 正式矩阵预览 100 个唯一 job、25 个配对块，正式完成 0。低、高负载尚未冻结。
- P2 已确认正式背景采用均匀排布。稀疏候选无法登记十对时记录几何不合格。见 docs/P2_场景与观测接口.md。课程项目目录目前拒绝新增归档，阶段交付采用版本化 ZIP。

P2 已通过。S0/S1/S2、有效漏收、同种子裁剪等价性和 16/64/160/480/960 五阶双方案资源试跑均有真实证据。480/G0 初次失败源于末时刻车辆按时驶离后被旧检查误判，保留原尝试并经 SUMO tripinfo 区分离场和截断。总验收见 verification/p2/acceptance.json。

P3 的 E0 密度校准入口是 experiment.ps1 -Action calibrate -Config configs/core.json。通过 MCP 按已登记顺序提交，进度在 results/calibration/。正式样本仍为 0。

P3 E0 正在执行。密度 5、预热 20 s 的两个 120 s run 已验收，种子 100 和 101 分别满足低档与稳定性判据。五窗口目标等权 CBR 各为 8.16%–8.23% 和 8.30%–8.43%。首条请求因 Windows 长临时路径未进入引擎，原证据已保留，修复仅缩短请求名，同科学配置重试成功。后续候选和当前任务以 results/calibration/core_v1/index.json 为准，冻结与正式启动须待 P3 其余门槛通过。

## 2026-11-24 02:55 状态

- 控制环修复完成（估计量 settle 1 s 门控）：启动饱和消除，预热期只测不控；独立校验器同步复刻，C++ 772 checks / Python 7 passed。
- 目标负载重标定：CBR 由车队自身信标产生（~0.04/Hz），`cbr_target=0.18` 时 d=33 稳定在 b≈3.4 Hz、CBR≈0.16；d=5 顶格 10 Hz。
- 两档校准成立：`results/calibration/core_v32/decision.json` → `selected {low: 5, high: 33}`（首次非 grid_exhausted）。
- S1 三种子结论：G2/G3 把规范定义的 D_info 从 161.8 ms 降到 100.4 ms（−38%），发送量变化 <0.02%；G3≡G2（H2 不成立）；全部在 0.8 s 期限内送达。
- 结构性限制：all_vehicles_same_scheme 下“可区分”与“有拥塞”互斥，见 `docs/E0_v3_两档设计_20261123.md` 第 11 节；v4 方向为背景定速 + 目标受控。
- 交付：GitHub `EIE575-Project` 已同步（commit a0bda08），含源码、配置、四张图与结果说明。

## 2026-11-24 13:10 状态（最终）

- v4 部署模式落地（背景定速 10 Hz、仅目标车受控）：E0 CBR 0.423、b 2.38 Hz；S1 6 次（G0/G2 × 3 种子）投递率 96%，G2 仍把事件起点时延从 244.8 ms 降到 116.6 ms（−52%），发送量不变、无超时。
- G1 速度加权基线补齐：事件起点 272.5 ms（比 G0 还差）、多发 20%——G2 相对 G0/G1 同时占优。
- 交付：`docs/REPORT_20261124.md`（完整报告）、`docs/deck/`（10 页 deck + pptx）、GitHub 已同步。
