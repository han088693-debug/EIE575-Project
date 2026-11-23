S1 基础场景

本目录只提供两车联动与急刹的运行验证场景，不代表已校准的拥塞实验。
使用 SUMO 1.6.0 的 netconvert 生成 highway.net.xml 后再运行。
在项目根目录执行：

netconvert --node-files scenarios/highway.nod.xml --edge-files scenarios/highway.edg.xml --output-file scenarios/highway.net.xml

道路自西向东，单车道 3000 m。lead 与 follow 从仿真时刻 0 起分别位于
70 m 与 40 m，车长 5 m，初始净距 25 m，最高速度和初始速度均为
22.2222 m/s。follow 使用 SUMO 默认跟驰；接收无线消息不影响驾驶。

BrakeScenario 在约 20 s 调用 lead 的 TraCI slowDown(0, 3.7037 s)，
过渡结束后 setSpeed(0) 保持停车。未关闭 SUMO 安全检查，实际制动曲线
以 scenario_truth.csv 为准，不把指令强度当成实测强度。

顶层网络需要同级子模块 manager 和 brakeScenario: tabr.BrakeScenario。
应用使用 tabr.TabrApp，场景模块不向应用暴露计划事件或真实 t0。
建议 playgroundSizeX=3100m，playgroundSizeY=100m，playgroundSizeZ=50m；
manager 的默认 margin=25 可容纳到此边界内。
manager.updateInterval 与 brakeScenario.updateInterval 均为 0.1 s。

launchd 文件和 SUMO 输入放在同一目录。omnetpp.ini 应引用
xmldoc("scenarios/s1.launchd.xml")。生成的 net.xml 不放在其他目录。
准备脚本应将 launchd.xml 的 basedir 替换为本机 scenarios 的绝对路径。
模板中的相对 basedir 只有在 sumo-launchd 进程工作目录为项目根目录时才有效；
copy 元素中必须保留文件名，Veins 5.0 不允许该属性包含斜杠。

输出目录由运行脚本预先创建，设定 outputDir 与 runId 后才运行。
event_log.csv 始终保留一个计划事件车对，初始化状态为 planned。
应用 event_id、t_detect 留空，由分析脚本从实际应用日志离线映射。
scenario_truth.csv 独立订阅 mobilityStateChanged，记录 lead/follow 实际轨迹。
t0 是命令后首个实际速度下降的离散采样时刻；t0_lower_bound 是前一采样
时刻。实际连续起点位于这两个时刻之间，0.1 s 步长限制时间分辨率。
不应把20 s计划时刻无条件当作实测t0，也不应直接将本场景结果当性能结论。

未找到车辆、更新步长不一致、未观察到制动、观测窗不完整会标记
scenario_error。异常事件不会从计划账本中消失。

后续需独立扩展背景负载并校准低高 CBR；S0先验和S2多车急刹未在本目录实现。
