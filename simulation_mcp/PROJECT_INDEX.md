# EIE575 仿真 MCP 入口

- 目标：通过本地 MCP 启动、监控、读日志、停止、验收现有 S1 仿真。
- 先读 `PROGRESS.md`；用户操作与五工具参数见 `README.md`；设计见 `docs/施工手册.md`。
- `server.py`：stdio 五工具；`client.py`：标准 MCP 协议调用入口。
- `controller.py`：校验、队列、状态、日志、结果；`host.py`：MCP 之外的常驻任务宿主。
- `worker.py`、`engine_entry.py`：工程快照与原 manage.run；`process_control.py`：Windows Job Object。
- `settings.json`：source_project/tools_root/state_root；`install.ps1`：正式安装、注册、启动宿主。
- `start_host.ps1`：普通 PowerShell 启动/复用宿主；机器重启后执行。
- 验证：`python -B -m unittest discover -s tests -p 'test_*.py'`；真实协议验证 `python -B tests/smoke_mcp.py`。
- 当前开发位置 `E:/Temp/eie575-simulation-mcp`；开发运行数据 `E:/Temp/eie575-sim-runtime`。正式安装位置由 install.ps1 指定。
