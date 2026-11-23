"""Run the P0 four-scheme S1 regression through an isolated, short-lived MCP host."""
import argparse
import asyncio
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import psutil


SCHEMES = ["G0", "G1", "G2", "G3"]
TOOLS = {"start_run", "get_status", "stop_run", "get_results", "tail_log"}
EXECUTABLE = "EIE575_TABR_Starter.exe"


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class ModuleEntry(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD), ("module_id", wintypes.DWORD), ("process_id", wintypes.DWORD),
        ("global_usage", wintypes.DWORD), ("process_usage", wintypes.DWORD),
        ("base_address", ctypes.c_void_p), ("base_size", wintypes.DWORD),
        ("module_handle", wintypes.HMODULE), ("module_name", wintypes.WCHAR * 256),
        ("path", wintypes.WCHAR * 260),
    ]


def loaded_modules(pid):
    """Toolhelp gives DLL image paths in the running OMNeT++ process."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateToolhelp32Snapshot
    create.argtypes, create.restype = [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE
    first, next_module = kernel.Module32FirstW, kernel.Module32NextW
    for fn in (first, next_module):
        fn.argtypes, fn.restype = [wintypes.HANDLE, ctypes.POINTER(ModuleEntry)], wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes, close.restype = [wintypes.HANDLE], wintypes.BOOL
    snap = create(0x08 | 0x10, pid)
    if snap == ctypes.c_void_p(-1).value:
        return []
    try:
        entry = ModuleEntry()
        entry.size = ctypes.sizeof(entry)
        result = []
        found = first(snap, ctypes.byref(entry))
        while found:
            result.append(Path(entry.path))
            found = next_module(snap, ctypes.byref(entry))
        return result
    finally:
        close(snap)


def watch_images(job_project, dependency_root, stop, captures, errors):
    expected = job_project / EXECUTABLE
    known = set()
    while not stop.is_set():
        try:
            for proc in psutil.process_iter(["name"]):
                if (proc.info["name"] or "").casefold() != EXECUTABLE.casefold():
                    continue
                try:
                    identity = (proc.pid, proc.create_time())
                    if identity in known or Path(proc.exe()) != expected:
                        continue
                    argv = proc.cmdline()
                    scheme = argv[argv.index("-c") + 1]
                    images = loaded_modules(proc.pid)
                    names = {p.name.lower() for p in images}
                    if not {"libveins.dll", "liboppsim.dll", "liboppcmdenv.dll"} <= names:
                        continue
                    captures.append({"scheme": scheme, "pid": proc.pid, "create_time": identity[1],
                                     "observed_epoch": time.time(), "executable": str(expected),
                                     "dependency_images": sorted(str(p) for p in images
                                          if p.is_relative_to(dependency_root) or p.name.lower() in
                                          {"libveins.dll", "liboppsim.dll", "liboppcmdenv.dll"})})
                    known.add(identity)
                except (psutil.Error, ValueError, IndexError, OSError):
                    continue
        except Exception as exc:
            errors.append(repr(exc))
            return
        stop.wait(0.035)


async def execute(args, state, out, host):
    from client import connect, result_data
    async with connect(args.mcp_dir / "server.py") as session:
        async def call(name, **params):
            return result_data(await session.call_tool(name, params))

        names = {tool.name for tool in (await session.list_tools()).tools}
        if names != TOOLS:
            raise RuntimeError(f"Unexpected tools: {names}")
        for _ in range(50):
            health = await call("get_status")
            if health["host"].get("ready"):
                pid = health["host"]["identity"]["pid"]
                if pid == host.pid or pid in {p.pid for p in psutil.Process(host.pid).children(recursive=True)}:
                    break
            if host.poll() is not None:
                raise RuntimeError(f"Owned host exited with {host.returncode}; see host.log")
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Owned host readiness timed out")
        write(out / "health.json", {"tools": sorted(names), **health})
        if args.check_host:
            return {"host_ready": True, "environment_ready": health["environment"]["ready"]}
        if not health["environment"]["ready"]:
            raise RuntimeError(f"Missing build or dependencies: {health['environment']}")
        job = await call("start_run", schemes=SCHEMES, seeds=[0], request_id="p0-4xseed0")
        job_id = job["job_id"]
        print(f"job {job_id}, state {state}", flush=True)
        job_dir = state / "jobs" / job_id
        project = job_dir / "project"
        stop = threading.Event()
        captures, errors = [], []
        watcher = threading.Thread(target=watch_images,
                args=(project, args.tools_root, stop, captures, errors), daemon=True)
        watcher.start()
        terminal = False
        try:
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                status = await call("get_status", job_id=job_id)
                if status["status"] in {"passed", "failed", "interrupted", "cancelled"}:
                    terminal = True
                    break
                await asyncio.sleep(0.12)
            if not terminal:
                raise TimeoutError(f"Batch {job_id} exceeded {args.timeout} s")
            result = await call("get_results", job_id=job_id)
            write(out / "results.json", result)
            write(out / "status.json", status)
            for summary in result["runs"]:
                index = summary["index"]
                detail = await call("get_results", job_id=job_id, run_index=index)
                write(out / f"mcp_{summary['scheme']}.json", detail)
                if summary.get("result_dir"):
                    folder = Path(summary["result_dir"]).resolve()
                    if folder.parent != project / "results":
                        raise RuntimeError("Result outside owned run snapshot")
                    shutil.copytree(folder, out / "runs" / summary["scheme"])
            if result["complete"] is not True or [r["scheme"] for r in result["runs"]] != SCHEMES:
                raise RuntimeError("MCP batch incomplete; see results.json")
        finally:
            if not terminal:
                await call("stop_run", job_id=job_id)
            stop.set()
            watcher.join(timeout=3)
            write(out / "module_observations.json", {"processes": captures, "errors": errors})
            for log in job_dir.glob("*.log"):
                shutil.copy2(log, out / log.name)
            for name in ("job.json", "input_hashes.json"):
                if (job_dir / name).is_file():
                    shutil.copy2(job_dir / name, out / name)
        hashes = json.loads((out / "input_hashes.json").read_text(encoding="utf-8"))
        if hashes[EXECUTABLE] != sha256(args.project / EXECUTABLE) or hashes[EXECUTABLE] != sha256(project / EXECUTABLE):
            raise RuntimeError("Built executable differs from the executed snapshot")
        if {c["scheme"] for c in captures} != set(SCHEMES) or errors:
            raise RuntimeError("DLL image evidence missing; see module_observations.json")
        dlls = {x for c in captures for x in c["dependency_images"]}
        write(out / "loaded_dll_sha256.json", {p: sha256(Path(p)) for p in sorted(dlls)})
        inputs = out / "effective_inputs"
        shutil.copytree(project / "scenarios", inputs / "scenarios")
        shutil.copytree(project / "generated", inputs / "generated")
        for name in ("omnetpp.ini", "radio.xml"):
            shutil.copy2(project / name, inputs / name)
        return {"passed": True, "job_id": job_id, "seed": 0,
                "checks": {r["scheme"]: r["summary"]["checks_total"] for r in result["runs"]},
                "observed_omnet_processes": len(captures), "dependency_images": len(dlls),
                "executable_sha256": hashes[EXECUTABLE]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--mcp-dir", type=Path, default=Path("E:/港理课程/课程文件/eie575/project/simulation_mcp"))
    parser.add_argument("--tools-root", type=Path, default=Path("E:/EIE575_Tools"))
    parser.add_argument("--state-parent", type=Path, default=Path("E:/Temp"))
    parser.add_argument("--out", type=Path, required=True, help="New evidence directory")
    parser.add_argument("--check-host", action="store_true", help="Probe an isolated host without running a simulation")
    parser.add_argument("--timeout", type=int, default=180, help="Overall regression timeout (wall seconds)")
    args = parser.parse_args()
    args.project, args.mcp_dir, args.tools_root = [p.resolve() for p in (args.project, args.mcp_dir, args.tools_root)]
    state = Path(tempfile.mkdtemp(prefix="etp0_", dir=args.state_parent.resolve()))
    if not str(state).isascii() or " " in str(state):
        parser.error("Use an ASCII state-parent without spaces")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    settings = state / "settings.json"
    write(settings, {"source_project": str(args.project), "tools_root": str(args.tools_root),
                     "state_root": str(state), "max_run_wall_s": max(30, args.timeout)})
    shutil.copy2(settings, out / settings.name)
    os.environ["EIE575_MCP_SETTINGS"] = str(settings)
    sys.path.insert(0, str(args.mcp_dir))
    with (out / "host.log").open("wb") as log:
        host = subprocess.Popen([sys.executable, "-B", "-u", "-X", "utf8", str(args.mcp_dir / "host.py"), "--serve"],
                cwd=args.mcp_dir, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            record = asyncio.run(execute(args, state, out, host))
        finally:
            if host.poll() is None:
                # On Windows a venv redirector can create the actual interpreter as its child.
                for child in psutil.Process(host.pid).children(recursive=True):
                    try:
                        child.terminate()
                    except psutil.NoSuchProcess:
                        pass
                if host.poll() is None:
                    host.terminate()
            host.wait(timeout=10)
    write(out / "report.json", record)
    print(json.dumps(record, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
