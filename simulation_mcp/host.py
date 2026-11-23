"""A local task host started outside the MCP client's process tree."""
import argparse
import json
import msvcrt
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

import psutil
from config import HERE, Settings
from controller import Controller, TERMINAL, read_json, save_json, start_lock, utc_now
from process_control import process_alive


def launch_worker(directory, job):
    process = None
    try:
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        with (directory / "worker.log").open("ab", buffering=0) as output:
            process = subprocess.Popen([sys.executable, "-B", "-u", "-X", "utf8", str(HERE / "worker.py"), str(directory)],
                stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT, cwd=HERE, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        job["worker"] = {"pid": process.pid, "create_time": psutil.Process(process.pid).create_time()}
        save_json(directory / "job.json", job)
        process.stdin.write(b"GO\n")
        process.stdin.flush()
        process.stdin.close()
    except Exception as error:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        job.update(status="failed", error=f"Cannot start worker: {error}", finished_epoch=time.time())
        save_json(directory / "job.json", job)
        traceback.print_exc()


def serve():
    controller = Controller()
    root = controller.root
    root.mkdir(parents=True, exist_ok=True)
    with (root / "host.lock").open("a+b") as lock:
        if lock.seek(0, 2) == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return 0  # Another host already owns this state root.
        state = {"identity": {"pid": os.getpid(), "create_time": psutil.Process().create_time()},
                 "started_at": utc_now(), "server_directory": str(HERE)}
        last_heartbeat = 0
        try:
            while True:
                if time.monotonic() - last_heartbeat >= 1:
                    state["heartbeat_epoch"] = time.time()
                    save_json(root / "host.json", state)
                    last_heartbeat = time.monotonic()
                try:
                    with start_lock(root):
                        jobs = controller._all_jobs()
                        if not any(job["status"] not in TERMINAL and process_alive(job.get("worker")) for job in jobs):
                            for job in reversed(jobs):
                                if job["status"] == "queued" and not job.get("worker"):
                                    launch_worker(controller.job_dir(job["job_id"]), job)
                                    break
                except Exception:
                    traceback.print_exc()
                time.sleep(0.2)
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def start():
    controller = Controller()
    state = controller.host_status()
    if state["ready"]:
        print(json.dumps(state, ensure_ascii=False))
        return 0
    if state.get("alive"):
        raise RuntimeError("An existing host is alive but has a stale heartbeat; inspect host.log")
    controller.root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    with (controller.root / "host.log").open("ab", buffering=0) as output:
        process = subprocess.Popen([sys.executable, "-B", "-u", "-X", "utf8", str(HERE / "host.py"), "--serve"],
            cwd=HERE, env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = controller.host_status()
        if state["ready"]:
            print(json.dumps(state, ensure_ascii=False))
            return 0
        if process.poll() is not None:
            raise RuntimeError("Host failed to start; inspect " + str(controller.root / "host.log"))
        time.sleep(0.1)
    raise RuntimeError("Host readiness timed out; inspect " + str(controller.root / "host.log"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true", help="Run in foreground; normally use --start")
    parser.add_argument("--start", action="store_true", help="Start a hidden host from an ordinary shell, outside MCP")
    args = parser.parse_args()
    if not (args.serve ^ args.start):
        parser.error("Select exactly one of --start or --serve")
    sys.exit(serve() if args.serve else start())
