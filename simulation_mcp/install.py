"""Install the EIE575 MCP package from a normal, user-owned shell.

This script never changes permissions, elevates privileges, or launches the host.
Configuration and package updates are preflighted before the first write.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import tomllib
import uuid

INSTALLER_ID = "eie575-simulation-mcp/install-v1"
MARKER_NAME = ".eie575-mcp-install.json"
SERVER_NAME = "eie575-simulation"
DEFAULT_INSTALL = Path(r"E:\港理课程\课程文件\eie575\project\simulation_mcp")
DEFAULT_STATE = Path(r"E:\EIE575_Tools\workspace\eie575_mcp_runs")
DEFAULT_CONFIG = Path(r"E:\港理课程\.codex\config.toml")
DEFAULT_PYTHON = Path(r"E:\港理课程\.venvs\courseware-mcp\Scripts\python.exe")
EXCLUDED_DIRS = {"__pycache__", ".git", ".venv", "evidence", "results", "runs", "runtime", "state", "dist", "build"}


class InstallError(RuntimeError):
    pass


def _same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def _read_marker(directory: Path) -> dict | None:
    marker = directory / MARKER_NAME
    if not marker.is_file():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise InstallError("安装所有权标记无效；未覆盖该目录。") from exc
    if (not isinstance(data, dict) or data.get("installer_id") != INSTALLER_ID
            or not isinstance(data.get("install_dir"), str)
            or not _same_path(data["install_dir"], directory)):
        raise InstallError("目标目录不属于本安装器；未覆盖该目录。")
    return data


def _source_files(source: Path) -> list[Path]:
    if not (source / "server.py").is_file() or not (source / "settings.json").is_file():
        raise InstallError("安装包缺少 server.py 或 settings.json。")
    selected: list[Path] = []
    for item in sorted(source.iterdir()):
        if item.is_file() and not item.name.startswith(".") and (
            item.suffix.lower() in {".py", ".json", ".ps1"}
            or item.name in {"requirements.txt", "README.md", "PROJECT_INDEX.md", "PROGRESS.md"}
        ):
            selected.append(item)
    for dirname in ("docs", "tests"):
        folder = source / dirname
        if folder.is_dir():
            selected.extend(path for path in sorted(folder.rglob("*"))
                            if path.is_file() and not any(part in EXCLUDED_DIRS or part.startswith(".")
                                                          for part in path.relative_to(folder).parts))
    for path in selected:
        relative = path.relative_to(source)
        if any((source.joinpath(*relative.parts[:index])).is_symlink()
               for index in range(1, len(relative.parts) + 1)):
            raise InstallError("安装包包含符号链接；请使用完整解压的普通文件。")
    return selected


def _multiline_state(line: str, state: str | None) -> str | None:
    """Track TOML multiline strings so fake table headings inside them are ignored."""
    index = 0
    while index < len(line):
        if state:
            if line.startswith(state, index):
                index += 3
                state = None
            elif state == '"""' and line[index] == "\\":
                index += 2
            else:
                index += 1
            continue
        char = line[index]
        if char == "#":
            break
        if line.startswith('"""', index) or line.startswith("'''", index):
            state = line[index:index + 3]
            index += 3
        elif char in "\"'":
            quote = char
            index += 1
            while index < len(line):
                if line[index] == quote:
                    index += 1
                    break
                if quote == '"' and line[index] == "\\":
                    index += 2
                else:
                    index += 1
        else:
            index += 1
    return state


def _table_path(line: str) -> tuple[str, ...] | None:
    if not re.match(r"^\s*\[", line):
        return None
    try:
        value = tomllib.loads(line)
    except tomllib.TOMLDecodeError:
        return None
    path = []
    while isinstance(value, dict) and len(value) == 1:
        key, value = next(iter(value.items()))
        path.append(key)
    if value == {} or isinstance(value, list):
        return tuple(path)
    return None


def _remove_service_sections(text: str) -> str:
    output = []
    skip = False
    state = None
    for line in text.splitlines(keepends=True):
        heading = _table_path(line) if state is None else None
        if heading is not None:
            skip = len(heading) >= 2 and heading[:2] == ("mcp_servers", SERVER_NAME)
        if not skip:
            output.append(line)
        state = _multiline_state(line, state)
    return "".join(output)


def _without_service(parsed: dict) -> dict:
    result = copy.deepcopy(parsed)
    servers = result.get("mcp_servers")
    if isinstance(servers, dict):
        servers.pop(SERVER_NAME, None)
        if not servers:
            result.pop("mcp_servers")
    return result


def _build_config(original: str, destination: Path, python: Path, marker: dict | None) -> tuple[str, dict]:
    try:
        parsed = tomllib.loads(original)
    except tomllib.TOMLDecodeError as exc:
        raise InstallError("现有 MCP 配置不是有效 TOML；未修改配置。") from exc
    servers = parsed.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise InstallError("现有 mcp_servers 配置格式不兼容；未修改配置。")
    previous = servers.get(SERVER_NAME)
    expected_server = destination / "server.py"
    if previous is not None:
        previous_args = previous.get("args") if isinstance(previous, dict) else None
        owned = (marker is not None and isinstance(previous, dict)
                 and isinstance(previous.get("command"), str)
                 and _same_path(previous["command"], python)
                 and isinstance(previous_args, list) and len(previous_args) == 4
                 and previous_args[:3] == ["-B", "-X", "utf8"]
                 and isinstance(previous_args[3], str)
                 and _same_path(previous_args[3], expected_server))
        if not owned:
            raise InstallError("已有 eie575-simulation 服务来自其他安装或无所有权标记；未替换。")
    new_service = {
        "command": str(python),
        "args": ["-B", "-X", "utf8", str(expected_server)],
        "startup_timeout_sec": 45,
        "tool_timeout_sec": 120,
    }
    section = "\n[mcp_servers.eie575-simulation]\n" + "\n".join(
        f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in new_service.items()
    ) + "\n"
    cleaned = _remove_service_sections(original) if previous is not None else original
    candidate = cleaned.rstrip("\r\n") + "\n" + section
    try:
        verified = tomllib.loads(candidate)
    except tomllib.TOMLDecodeError as exc:
        raise InstallError("配置使用不兼容的内联或点分写法；为保留原内容，未修改配置。") from exc
    if (_without_service(verified) != _without_service(parsed)
            or verified.get("mcp_servers", {}).get(SERVER_NAME) != new_service):
        raise InstallError("配置保留验证失败；未修改配置。")
    return candidate, new_service


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix="." + path.name + ".", suffix=".tmp",
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def install_package(*, source_dir: Path, install_dir: Path, state_root: Path,
                    config_file: Path, python_executable: Path = DEFAULT_PYTHON) -> dict:
    source = Path(source_dir).resolve()
    destination = Path(install_dir).resolve()
    state = Path(state_root).resolve()
    config = Path(config_file).resolve()
    python = Path(python_executable).resolve()
    if not python.is_file():
        raise InstallError("指定的 Python 解释器不存在。")
    if destination.exists() and not destination.is_dir():
        raise InstallError("安装目标不是目录。")
    marker = _read_marker(destination) if destination.is_dir() else None
    if (destination.is_dir() and any(destination.iterdir()) and marker is None
            and not _same_path(source, destination)):
        raise InstallError("安装目标是非空且不属于本安装器的目录；未覆盖。")
    files = _source_files(source)
    try:
        settings = json.loads((source / "settings.json").read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        raise InstallError("安装包 settings.json 无法读取。") from exc
    if not isinstance(settings, dict):
        raise InstallError("安装包 settings.json 必须为对象。")
    settings["state_root"] = str(state)
    config_existed = config.exists()
    original_bytes = config.read_bytes() if config_existed else b""
    try:
        original = original_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InstallError("配置文件不是 UTF-8；未修改配置。") from exc
    config_text, service = _build_config(original, destination, python, marker)
    planned = [(path.relative_to(source), path.read_bytes()) for path in files]
    for relative, _ in planned:
        target = destination / relative
        if target.is_symlink() or any(parent.is_symlink() for parent in target.parents
                                      if parent != destination and destination in parent.parents):
            raise InstallError("安装目标包含符号链接；未覆盖。")
        if target.exists() and not target.is_file():
            raise InstallError("安装目标存在同名目录；未覆盖。")
    # All path/ownership/configuration checks above are read-only.
    destination.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    for relative, content in planned:
        if relative == Path("settings.json"):
            content = (json.dumps(settings, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        target = destination / relative
        if not target.is_file() or target.read_bytes() != content:
            _atomic_write(target, content)
    owned_files = sorted({str(relative) for relative, _ in planned}
                         | set(marker.get("installed_files", []) if marker else []))
    ownership = {"installer_id": INSTALLER_ID, "install_dir": str(destination),
                 "installed_files": owned_files, "server": SERVER_NAME}
    _atomic_write(destination / MARKER_NAME,
                  (json.dumps(ownership, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    backup = None
    new_bytes = config_text.encode("utf-8")
    if original_bytes != new_bytes:
        config.parent.mkdir(parents=True, exist_ok=True)
        if config.exists() != config_existed or (config_existed and config.read_bytes() != original_bytes):
            raise InstallError("配置文件在安装期间被其他程序修改；已保留新配置，请重试。")
        # Preserve the exact existing bytes before replacing the configuration.
        if config.exists():
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = config.with_name(config.name + ".backup-" + stamp + "-" + uuid.uuid4().hex[:8])
            with backup.open("xb") as stream:
                stream.write(original_bytes)
                stream.flush()
                os.fsync(stream.fileno())
        _atomic_write(config, new_bytes)
    return {"install_dir": str(destination), "state_root": str(state),
            "config_file": str(config), "config_backup": str(backup) if backup else None,
            "service": SERVER_NAME, "copied_files": len(planned)}


def main() -> int:
    parser = argparse.ArgumentParser(description="安装并注册 EIE575 仿真本地 MCP；不提权，不修改目录权限。")
    parser.add_argument("--install-dir", type=Path, default=DEFAULT_INSTALL)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--config-file", type=Path, default=DEFAULT_CONFIG)
    options = parser.parse_args()
    try:
        result = install_package(source_dir=Path(__file__).parent, install_dir=options.install_dir,
                                 state_root=options.state_root, config_file=options.config_file)
    except (InstallError, OSError) as exc:
        print(f"安装未完成：{exc}", file=sys.stderr)
        print("未修改权限。请在你有写入权限的普通 PowerShell 中运行，或用参数指定可写路径。", file=sys.stderr)
        return 1
    print("安装及 MCP 配置注册完成。")
    for key, label in (("install_dir", "程序目录"), ("state_root", "运行数据"),
                       ("config_file", "MCP 配置"), ("config_backup", "原配置备份")):
        if result[key]:
            print(f"{label}：{result[key]}")
    print("请启动独立宿主（start_host.ps1），然后重载本地 MCP 连接或重新打开本课程项目。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
