# EIE575-Project

EIE575 课程项目文件归档（TABR 紧急制动仿真 / OMNeT++ 场景 / 仿真 MCP 工具链）。

## 仓库内容

| 文件 | 大小 | 说明 |
| --- | --- | --- |
| `EIE575_Project_MCP_及使用说明_20261123.zip` | 866 KB | 项目源码 + 仿真 MCP 工具 + 使用说明，共 170 个文件 |

压缩包内目录结构：

- `TABR_project/` — 主项目：`configs/`（实验配置与 schema）、`tools/`（实验、标定、分析脚本）、`docs/`、仿真可执行文件
- `S1_starter/` — OMNeT++ 起步工程：`src/`（TabrApp、BrakeScenario 等）、`scenarios/`（SUMO 路网与路由）、`tools/`、`Makefile`、`omnetpp.ini`
- `simulation_mcp/` — 仿真 MCP 服务：`server.py`、`controller.py`、`host.py`、`worker.py`、`install.py`、`tests/`、`evidence/`
- 根目录另有 MCP 使用说明与执行计划 Markdown 文档

## 获取与解压

```bash
git clone https://github.com/han088693-debug/EIE575-Project.git
cd EIE575-Project
unzip "EIE575_Project_MCP_及使用说明_20261123.zip"
```

> 压缩包内中文文件名以 GBK 存储，Windows 资源管理器 / 7-Zip 直接解压即可；Linux 下如出现乱码，可用 `unzip -O cp936` 或 `7z x` 解压。

## 校验

```
SHA256: c442aaea09065714e570d826f787e273623b8ce3bb76eaf0110c6d5aa91642c2
```
