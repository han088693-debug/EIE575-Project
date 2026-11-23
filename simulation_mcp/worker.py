"""Detached owner of a serial batch. All simulation processes belong to its Job Object."""
import hashlib
import os
from pathlib import Path
import shutil
import sys
import time
import traceback

from config import HERE, Settings
from controller import read_json, save_json, utc_now
from process_control import OwnedProcess


def snapshot(source, destination):
    destination.mkdir()
    for name in ("EIE575_TABR_Starter.exe", "omnetpp.ini", "radio.xml"):
        shutil.copy2(source / name, destination / name)
    for name in ("src", "scenarios", "tools"):
        shutil.copytree(source / name, destination / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    hashes = {}
    for path in sorted(destination.rglob("*")):
        if path.is_file():
            hashes[path.relative_to(destination).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def run(directory):
    manifest = directory / "job.json"
    job = read_json(manifest)
    settings = Settings(**{key: Path(value) if key.endswith("root") or key == "source_project" else value
                           for key, value in job["settings"].items()})
    project = directory / "project"
    cancel = directory / "cancel.request"
    owned = None
    current = None

    def save():
        job["heartbeat_epoch"] = time.time()
        save_json(manifest, job)

    try:
        job["status"] = "running"
        job["stage"] = "snapshot"
        save()
        hashes = snapshot(settings.source_project, project)
        save_json(directory / "input_hashes.json", hashes)
        job["input_hashes_file"] = str(directory / "input_hashes.json")
        job["stage"] = "simulation"
        save()
        for current in job["runs"]:
            if cancel.exists():
                job["status"] = "cancelled"
                break
            index = current["index"]
            job["current_run"] = index
            current.update(status="running", started_at=utc_now(), started_epoch=time.time())
            save()
            known = set((project / "results").glob("*")) if (project / "results").is_dir() else set()
            result_file = directory / f"engine_{index:03d}.json"
            env = os.environ.copy()
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["PYTHONUNBUFFERED"] = "1"
            command = [sys.executable, "-B", "-u", "-X", "utf8", str(HERE / "engine_entry.py"),
                       "--project", str(project), "--tools", str(settings.tools_root),
                       "--scheme", current["scheme"], "--seed", str(current["seed"]),
                       "--result-file", str(result_file)]
            started = time.monotonic()
            with (directory / f"driver_{index:03d}.log").open("wb", buffering=0) as output:
                owned = OwnedProcess(command, cwd=project, env=env, output=output)
                job["engine"] = owned.identity
                save()
                last_save = time.monotonic()
                while owned.poll() is None:
                    if not current.get("result_dir"):
                        new = [p for p in (project / "results").glob("*") if p not in known and p.is_dir() and (p / "run.json").is_file()]
                        if len(new) == 1:
                            current["result_dir"] = str(new[0])
                            save()
                    if cancel.exists():
                        owned.stop()
                        current["status"] = "cancelled"
                        job["status"] = "cancelled"
                        break
                    if time.monotonic() - started > settings.max_run_wall_s:
                        owned.stop()
                        raise RuntimeError(f"Run exceeded {settings.max_run_wall_s} wall-clock seconds")
                    if time.monotonic() - last_save >= 1:
                        save()
                        last_save = time.monotonic()
                    time.sleep(0.1)
                code = owned.process.wait(timeout=10)
                owned.close()
                owned = None
            # A fast failure may have created its logs after the last polling tick.
            if not current.get("result_dir"):
                new = [p for p in (project / "results").glob("*") if p not in known and p.is_dir() and (p / "run.json").is_file()]
                if len(new) == 1:
                    current["result_dir"] = str(new[0])
            job["engine"] = None
            current["exit_code"] = code
            current["elapsed_s"] = round(time.monotonic() - started, 3)
            current["finished_at"] = utc_now()
            if job["status"] == "cancelled":
                save()
                break
            receipt = read_json(result_file) if result_file.is_file() else {}
            if receipt.get("result_dir"):
                current["result_dir"] = receipt["result_dir"]
            if code != 0 or not receipt.get("success"):
                raise RuntimeError(receipt.get("error", f"Engine exit {code}; inspect driver log"))
            if not current.get("result_dir") or not (Path(current["result_dir"]) / "summary.json").is_file():
                raise RuntimeError("Engine returned success without an analysis summary")
            if read_json(Path(current["result_dir"]) / "summary.json").get("passed") is not True:
                raise RuntimeError("Analysis summary did not pass validation")
            current["status"] = "passed"
            save()
        else:
            job["status"] = "passed"
    except BaseException as error:
        traceback.print_exc()
        job["status"] = "failed"
        job["error"] = str(error)
        if current is not None and current["status"] == "running":
            current["status"] = "failed"
            current["error"] = str(error)
    finally:
        if owned is not None:
            try:
                owned.stop()
            finally:
                owned.close()
        job["engine"] = None
        for entry in job["runs"]:
            if entry["status"] == "pending":
                entry["status"] = "skipped"
        job["stage"] = "finished"
        job["finished_epoch"] = time.time()
        job["finished_at"] = utc_now()
        save()


if __name__ == "__main__":
    if sys.stdin.buffer.readline() != b"GO\n":
        raise RuntimeError("Worker startup was not committed")
    run(Path(sys.argv[1]).resolve())
