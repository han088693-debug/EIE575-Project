# EIE575 突发制动项目（TABR 主线工程）

本目录是 TABR 主线仿真工程：OMNeT++ 5.6.2 + Veins 5.0 + SUMO 1.6.0，IEEE 802.11p 单信道。研究结论、指标口径与全部实验数字见 [REPORT_20261124.md](docs/REPORT_20261124.md)。本 README 只回答三件事：怎么构建、怎么跑、日志是什么口径。

## 当前状态

- 四个策略 G0–G3 已实现，并在 S1 主实验（4 策略 × 3 种子）与 v4 拥塞验证（G0/G2 × 3 种子）中跑完，结论见报告。
- 早期「v0.2 源码分发版」「S1 两车功能场景待扩展」的描述已删除，避免与报告口径冲突；历史版本见 git 记录。
- 正式的 100 次矩阵未执行，原因是部署模型的设计限制，见报告第 6 节。

## 环境

- 工具根：E:\EIE575_Tools\omnetpp-5.6.2、veins-5.0、sumo-1.6.0
- 编译与运行请用纯英文路径；课程目录里只保留源码副本
- 用 OMNeT++ 自带的 Python 2.7 运行 Veins 启动器；辅助 Python 脚本兼容 2.7 与 3

## 最短运行步骤

    powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 -Action build
    powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 -Scheme G2 -Seed 300
    powershell -NoProfile -ExecutionPolicy Bypass -File .\batch.ps1

- 首次使用先 build；run.ps1 会生成与本机路径匹配的 Makefile，并重链接 Veins 动态库
- 批量脚本串行跑 G0–G3 × 种子 0–4，只作功能验证，不计入正式矩阵
- 需要图形窗口时在命令末尾加 -Gui

## 文件分工

| 文件 | 用途 |
| --- | --- |
| src/TabrApp.*、src/TabrCore.h | 应用、控制器、发送定时器、历史窗口与速率公式 |
| src/BrakeScenario.*、src/FormalScenario.* | 早期两车场景与正式场景 |
| src/FormalObservation.* | 正式观测模块与日志输出 |
| configs/ | core、core_v3、core_v4 与探针配置 |
| tools/ | 引擎、校验器、分析器、校准与运行器 |
| tests/ | 不依赖仿真引擎的核心回归检查 |
| scenarios/ | SUMO 道路输入、路线与 launchd 模板 |

## 日志口径

- event_log.csv：预登记事件；实际 t0 与观察窗状态
- scenario_truth.csv：独立模块采到的真实运动
- control_log.csv：固定周期 C、平滑 Cbar、e_H、B、S、b、r，末尾附 speed_mps 与最终倍率 W
- tx_log / rx_log：应用提交时刻与接收结果（tx 不等于无线发射时刻）
- busy_audit.csv：MAC 忙闲切换，可重新积分验证 CBR
- 时间为 s，距离 m，速度 m/s，加速度 m/s²；无事件的包用 event_id=-1 表示

## 故障定位

- 路径含中文或空格导致构建失败：换到纯英文工作目录
- 缺 opp_makemake 或 veins 库：先完成对应环境的 release 构建
- 仿真失败：看本次 simulation.log 与 launchd.log，非零退出不能记为通过
- 修改消息字段后重新 build，不要手改自动生成的 _m.h / _m.cc

## 来源

无线设置参考 EIE575_Lab2_20261014；工具版本来自课程原始压缩包与 SUMO 1.6.0 官方发布。新增代码遵循 Veins 的 GPL-2.0-or-later 与 OMNeT++ 随附许可。
