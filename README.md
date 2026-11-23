# EIE575-Project

EIE575 课程项目文件归档（TABR 紧急制动仿真 / OMNeT++ 场景 / 仿真 MCP 工具链）。

## 仓库内容

| 文件 | 大小 | 说明 |
| --- | --- | --- |
| `EIE575_Project_MCP_及使用说明_20261123.zip` | 866 KB | 项目源码 + 仿真 MCP + 使用说明，共 170 个文件 |
| `EIE575_前向升级.zip` | 95 KB | 前向升级包：README、patch、install.py、release.json、verify.py |

压缩包内目录结构（`EIE575_Project_MCP_及使用说明_20261123.zip`）：

- `TABR_project/` — 主项目：`configs/`（实验配置与 schema）、`tools/`（实验、标定、分析脚本）、`docs/`、仿真可执行文件
- `S1_starter/` — OMNeT++ 起步工程：`src/`（TabrApp、BrakeScenario 等）、`scenarios/`（SUMO 路网与路由）、`tools/`、`Makefile`、`omnetpp.ini`
- `simulation_mcp/` — 仿真 MCP 服务：`server.py`、`controller.py`、`host.py`、`worker.py`、`install.py`、`tests/`、`evidence/`
- 根目录另有 MCP 使用说明与执行计划 Markdown 文档

## 完整实验包

`EIE575_实验完整包_20261123.zip`（903 MB）体积超出 Git 单文件 100 MB 上限，放在 Release 附件：

<https://github.com/han088693-debug/EIE575-Project/releases/tag/v2026.11.23>

## 获取

```bash
git clone https://github.com/han088693-debug/EIE575-Project.git
cd EIE575-Project
unzip "EIE575_Project_MCP_及使用说明_20261123.zip"
```

> 压缩包内中文文件名以 GBK 存储，Windows 资源管理器 / 7-Zip 直接解压即可；Linux 下如出现乱码，可用 `unzip -O cp936` 或 `7z x` 解压。

## 校验

```
SHA256  EIE575_Project_MCP_及使用说明_20261123.zip  c442aaea09065714e570d826f787e273623b8ce3bb76eaf0110c6d5aa91642c2
SHA256  EIE575_前向升级.zip                          94f2bc07f044f43c204c527e7159f8c7849fd4d45dfb71fe32e1c9158d7f6f82
SHA256  EIE575_实验完整包_20261123.zip               35d75a1fd7d3add1fdeedd6900353344837f6cbcc2294a4818a925516bc47208
```
