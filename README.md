# EIE575-Project

EIE575 课程项目：TABR 紧急制动消息（V2X）仿真 + 自研 MCP 仿真工具链。

**先看 → [`进度说明.md`](进度说明.md)**：做了什么、做到哪一步、还剩什么没做，一页看完。

## 仓库结构

| 路径 | 内容 |
| --- | --- |
| [`进度说明.md`](进度说明.md) | 项目进度、已完成项、未完成项、验证方式 |
| [`EIE575_剩余工作与执行计划_20261123.md`](EIE575_剩余工作与执行计划_20261123.md) | 剩余工作排期与验收条件 |
| [`TABR_project/`](TABR_project) | 主线运行器、场景、配置、预编译模拟器与源码 |
| [`S1_starter/`](S1_starter) | OMNeT++ 起步工程（TabrApp、BrakeScenario、SUMO 路网） |
| [`simulation_mcp/`](simulation_mcp) | 仿真 MCP 服务：宿主、控制器、安装器、客户端、测试与验收证据 |
| [`实验归档/`](实验归档) | 校准结论、补充实验、运行器更新记录、CSV/JSON/PDF 与恢复脚本 |
| [`从这里开始.md`](从这里开始.md) / [`正式实验MCP使用说明.md`](正式实验MCP使用说明.md) | 上手与 MCP 调用说明 |
| `*.zip` | 原始交付压缩包（内容已解压到上述目录，保留用于校验） |

## 完整实验数据

原始数据 ZIP（约 900 MB）超出 Git 单文件 100 MB 上限，放在 Release 附件：

<https://github.com/han088693-debug/EIE575-Project/releases/tag/v2026.11.23>

## 快速开始

```bash
git clone https://github.com/han088693-debug/EIE575-Project.git
cd EIE575-Project
unzip "EIE575_Project_MCP_及使用说明_20261123.zip" -d EIE575MCP
```

压缩包内中文文件名以 GBK 存储，Windows 资源管理器 / 7-Zip 直接解压即可；Linux 下如出现乱码，可用 `unzip -O cp936` 或 `7z x`。

## 校验

```
SHA256  EIE575_Project_MCP_及使用说明_20261123.zip  c442aaea09065714e570d826f787e273623b8ce3bb76eaf0110c6d5aa91642c2
SHA256  EIE575_前向升级.zip                          94f2bc07f044f43c204c527e7159f8c7849fd4d45dfb71fe32e1c9158d7f6f82
SHA256  EIE575_实验完整包_20261123.zip               35d75a1fd7d3add1fdeedd6900353344837f6cbcc2294a4818a925516bc47208
```
