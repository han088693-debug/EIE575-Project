# EIE575 突发制动项目基础代码

这份工程供五位组员共用，当前源码分发版为 v0.2，配合《五人共享版完整交接手册 v1.1》。
它以已跑通 Lab2 的无线参数为起点，加入自有应用、独立制动场景和测量日志。
原 Lab2 工程保持原样。完整项目设计以交接手册为准。
`docs/` 内附完整手册 v1.1 的 Word、PDF 和策略建议 v1.1；每位组员拿到同一个分发包即可。
手册描述完整研究目标，本 README 和 `verification/` 记录这次基础代码已实现、
已实测的范围。

## 当前范围

- S1 两车功能场景：3 km 直路，lead 在前、follow 在后；约 20 s 对 lead 发出制动指令。
- G0：仅 CBR 基准；G1：速度权重；G2：实测制动标志；G3：制动标志与固定窗口预测偏差。
- 0.1 s 独立控制周期；忙闲信号积分得到 CBR；单独发送定时器；1–10 Hz 实际提交间隔边界。
- 固定 H=0.5 s 的历史运动评分；应用通过本车运动检测制动，不读取场景的未来事件时刻。
- 独立场景真值、应用检测、收发、邻车状态和 CBR 原始忙闲日志。

两车场景只用于确认功能和数据链路。低信道负载下各方案可能都达到 10 Hz，
所以本场景不能证明 G3 优于其他方案。S0、S2、背景车流、负载校准和多随机种子正式实验
是组员接下来需要完成的工作；当前参数是起跑参数。

## G1 速度权重更新（2026-10-21）

G1 借用《Speed-Aware V2X Congestion Control》式（2）中速度因子的倒数，
直接计算最终发送频率倍率：

```text
v_ref = 33 km/h = 9.166666666666667 m/s
G0: W = 1
G1: W = clip(v / v_ref, 1, 1 + beta)
G2: W = 1 + beta * B
G3: W = 1 + beta * max(B, S)
r = clip(b * W, r_min, r_max)
```

四组继续使用相同的 CBR 反馈、基准范围和 1–10 Hz 限频；没有引入论文的车辆密度控制律。
主配置 beta=2，所有组的倍率范围均为 1–3。停车和不超过 33 km/h 时，G1 的 W=1；
66、80、120 km/h 分别对应 W=2、约 2.424、3。在相同 b=2 Hz 时，目标频率分别为
4、约 4.848、6 Hz。上下限是本项目适配，不是原论文给出的权重上下限。

99 km/h 及以上达到倍率上限；低负载下最终 10 Hz 限频也可能掩盖速度差异。
beta 敏感性会改变 G1 的上限：beta=1 时 80/120 km/h 都达到 W=2。
各车实际 b 仍由本地 CBR 决定，共用参数不等于严格相同负载，不能凭上述算例宣称性能提升。
参数 `speedScale` 已更名为 `speedReference`，外部自定义配置应同步更新。

与旧式 `1+2*clip(v/120,0,1)` 相比，80 km/h 的倍率仅提高约 3.9%，120 km/h 不变。
本次修订明确论文启发的速度对照基线，实际性能收益仍待正式实验验证。
新版实际验证见 `verification/g1_speed_revision_20261021/验证记录.md`；
该目录记录新公式下四组各一次的功能仿真，旧版证据仍保留其原版本含义。

控制日志保留 U 列，将其统一定义为等价诊断值 `(W-1)/beta`，beta=0 时记为 0；
G1 的 U 已不再是旧版 `clip(v/120 km/h,0,1)`。计算频率时直接使用 W，
不会再把 `v/v_ref` 送入 `1+beta*U`。日志末尾新增 `speed_mps,W`，便于核对实际倍率。
`verification/` 原有四组验证记录属于修改前版本，须与本次更新后的独立验证记录区分。

## 环境和目录

使用课程对应的 OMNeT++ 5.6.2、Veins 5.0、SUMO 1.6.0。
在本机统一放到：

```text
E:\EIE575_Tools\
    omnetpp-5.6.2\
    veins-5.0\
    sumo-1.6.0\
    workspace\EIE575_TABR_Starter\
```

使用 OMNeT++ 自带的 Python 2.7 运行 Veins 启动器，不需要另下载 Python，
也不替换电脑现有 Python。所有辅助 Python 文件均兼容 Python 2.7 和 Python 3。

**实际编译和运行请使用纯英文路径。** 课程文件夹内保存可分发源码；
把整个工程解压到上述 workspace 位置后再操作。其他组员可用 `-ToolsRoot`
指定自己的同结构安装目录。

## 最短运行步骤

在工程文件夹打开 PowerShell。首次使用先编译：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 -Action build
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 -Scheme G0 -Seed 0
```

同一随机种子的其他方案：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 -Scheme G1 -Seed 0
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 -Scheme G2 -Seed 0
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 -Scheme G3 -Seed 0
```

脚本自动生成道路、写入本机场景路径、启动独立本地 SUMO 服务、运行 30 s 仿真、
检查结果并停止本次服务。每次生成新的结果目录，不覆盖以前的实验。
需要图形窗口时在运行命令末尾加 `-Gui`，在 Qtenv 点击运行并在完成后退出窗口。
正常批量测试默认使用无窗口方式。

## 自动批量运行

完成首次 build 后，一条命令运行 G0–G3 × 种子 0–4，共 20 次：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\batch.ps1
```

这是当前 S1 两车、每次 30 s 的**功能批量测试**，不计入手册的正式 114 次实验矩阵。
脚本串行执行，自动记录每次真实耗时、剩余时间估计、结果目录和验收状态。
一旦仿真或结果检查失败就停止，保留失败证据和尚未运行的条目。
每次启动建立新的批次，不覆盖旧结果；重复启动会新跑一批，不会自动续跑。
批次清单位于 `results/batches/<批次>/batch.json`。

完整 114 次包含负载校准、S0/S1 和 S2 场景。背景车流、S0 和 S2 尚待实现及校准，
因此当前不能把这 20 次功能测试直接扩充、重命名成正式实验。
正式矩阵冻结后再按配置批量执行；运行时间须先测低/高负载代表配置。

只生成道路与本机 launchd 配置：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 -Action prepare
```

若要在 OMNeT++ IDE 中编辑，先导入工具目录的 veins 工程，再导入本工程；
构建配置选择 release。命令行脚本会生成与本机路径匹配的 Makefile。
`.project` 中保留 veins 工程引用。
首次及后续命令行构建会先重新链接 Veins 动态库，补齐课程旧版 Clang 在 Windows
上漏导出的继承类符号；不修改 Veins 源码。组员使用原版工具包时也包含这一步。

## 文件分工

| 文件 | 用途 |
|---|---|
| `src/TabrApp.*` | 应用、控制器、消息定时器、邻车状态 |
| `src/TabrCore.h` | 可独立测试的历史窗口、忙时积分、速率公式 |
| `src/TabrBeacon.msg` | 状态和事件消息字段 |
| `src/BrakeScenario.*` | 独立场景制动控制及真值记录 |
| `src/ResearchHighway.ned` | 仿真网络 |
| `omnetpp.ini` | G0–G3 选择与统一无线参数 |
| `scenarios/` | SUMO 道路输入、两车路线、launchd 模板 |
| `tools/manage.py` | 编译、准备和运行入口 |
| `batch.ps1`、`tools/batch.py` | 四方案和多种子的串行功能批量运行、计时及状态清单 |
| `tools/analyze.py` | 单次功能验收及指标 |
| `tests/` | 不依赖仿真引擎的核心回归检查 |

五个人各自拿到完整工程和完整交接手册，再按手册负责模块；
不要把工程裁成五份互相缺文件的版本。

## 日志口径

结果保存在 `results/<方案>_seed<种子>_<时间>/`，同时包含命令、种子、
`simulation.log`、SUMO 启动日志及 `summary.json`。

| 日志 | 口径 |
|---|---|
| `event_log.csv` | 预先登记的计划事件；实际 t0、观察窗、场景异常状态 |
| `scenario_truth.csv` | 独立模块采到的 lead/follow 真实运动 |
| `detection_log_<车>.csv` | 本车真正检测到的事件，独立于是否成功发送 |
| `control_log_<车>.csv` | 固定周期 C、平滑 Cbar、e_H、B、S、等价诊断 U、b、r；末尾附 speed_mps、最终倍率 W |
| `busy_audit_<车>.csv` | MAC 忙闲切换，可重新积分验证 CBR |
| `tx_log_<车>.csv` | 应用提交到 MAC 的时刻；不等于无线发射时刻 |
| `rx_log_<车>.csv` | 接收、状态是否更新、事件是否有效、无效原因 |
| `truth_log_<车>.csv` | 应用实际运动采样 |
| `state_log_<车>.csv` | 每个控制时刻的邻车估计；首次收到前标为 unknown |

单位为 s、m、m/s、m/s²；消息总长度设为 2000 bit。
没有事件的包用 `event_id=-1` 和时间 `-1` 表示，不能当作有效事件。
事件由 sender_id + event_id 唯一标识，重复广播不重复计事件。

t0 是首次观测到 lead 减速的采样时刻；`t0_lower_bound` 记录上一次采样时刻，
两者明确表达 0.1 s 运动更新带来的起点分辨率。t_detect 由应用独立测量。
`event_log.csv` 内 event_id/t_detect 预留为空，离线分析依据检测日志关联。
deadline_s=0.8 是截止时长，deadline_at 是绝对时刻。

D_info 以真实 t0 为起点，取 follow 首次收到有效事件的时刻；D_update 取
首次收到生成于 t0 及之后的有效 lead 状态。无接收保留为 null 并单独标记，
不记成零延迟。应用缓存只从消息更新；真值仅供离线评价。
当前分析器用于功能验收，正式论文统计还需按手册实现配对重复实验和置信区间。

## 核心检查

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\run_core_tests.ps1
```

覆盖跨窗口 CBR 积分、固定 H 运动历史、反馈死区、四组权重、发送频率上限、
降速重排及持续控制更新时发送不会被无限推迟。

## 故障定位

- 路径含中文或空格导致构建问题：先用上述英文工作目录，保留课程目录中的源码副本。
- 缺少 `opp_makemake` 或 `veins` 库：先完成对应环境的 release 构建。
- 仿真失败：查看本次 `simulation.log` 和 `launchd.log`；非零退出不能记为通过。
- 检查未通过：查看 `summary.json` 的具体检查项，不把失败运行混入正式结果。
- 修改包字段后：重新 build，自动生成的 `_m.h/.cc` 不手工修改。

## 来源

无线设置参考用户提供的 `EIE575_Lab2_20261014` 副本。
工具版本来自课程原始 OMNeT++、Veins 压缩包及 SUMO 官方 1.6.0 Windows 发布包。
本工程新增代码用于课程研究仿真；与 Veins 链接和分发时遵循 Veins 的 GPL-2.0-or-later
及 OMNeT++ 随附许可。完整工具包不包含在这份轻量源码分发包内。
