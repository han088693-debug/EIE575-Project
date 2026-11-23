"""Persistent task management behind the five MCP tools."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
import uuid

import psutil
from config import HERE, Settings
from process_control import process_alive

TERMINAL = {"passed", "failed", "cancelled", "interrupted"}
JOB_ID = re.compile(r"sim_[0-9]{8}T[0-9]{6}_[0-9a-f]{12}\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
        for attempt in range(10):
            try:
                os.replace(temp, path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.02)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


@contextmanager
def start_lock(root):
    import msvcrt
    root.mkdir(parents=True, exist_ok=True)
    with (root / "start.lock").open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + 5
        while True:
            try:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Another start request is in progress; retry")
                time.sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def tail_text(path, lines=80, byte_limit=65536):
    path = Path(path)
    if not path.is_file():
        return {"available": False, "text": "", "truncated": False}
    with path.open("rb") as stream:
        size = stream.seek(0, 2)
        offset = max(0, size - byte_limit)
        stream.seek(offset)
        text = stream.read(byte_limit).decode("utf-8", "replace")
    rows = text.splitlines()
    if offset and rows:
        rows = rows[1:]
    return {"available": True, "text": "\n".join(rows[-lines:]),
            "truncated": bool(offset or len(rows) > lines),
            "size_bytes": size, "modified_epoch": path.stat().st_mtime}


def parse_progress(text):
    matches = list(re.finditer(r"Event\s+#(\d+)\s+[Tt]=([\d.eE+\-]+)", text))
    if not matches:
        return None
    match = matches[-1]
    sim_time = float(match.group(2))
    return {"event_number": int(match.group(1)), "simulation_time_s": sim_time,
            "simulation_limit_s": 30.0, "percent": min(100.0, 100 * sim_time / 30)}


class Controller:
    def __init__(self, settings=None):
        self.settings = settings or Settings.load()
        self.root = self.settings.state_root

    def job_dir(self, job_id):
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise ValueError("Invalid job_id; use the ID returned by start_run")
        directory = self.root / "jobs" / job_id
        if not directory.is_dir():
            raise ValueError("Unknown job_id")
        return directory

    def _read(self, job_id):
        return read_json(self.job_dir(job_id) / "job.json")

    def _all_jobs(self):
        folder = self.root / "jobs"
        if not folder.is_dir():
            return []
        rows = []
        for path in sorted(folder.glob("sim_*/job.json"), reverse=True):
            # Fail closed on a corrupt manifest: do not start overlapping work.
            try:
                rows.append(read_json(path))
            except (OSError, ValueError) as error:
                raise RuntimeError(f"Cannot read task manifest {path}: {error}") from error
        return rows

    def host_status(self):
        path = self.root / "host.json"
        if not path.is_file():
            return {"ready": False, "message": "Run start_host.ps1 from a normal PowerShell session first"}
        try:
            host = read_json(path)
            age = time.time() - host["heartbeat_epoch"]
            alive = process_alive(host.get("identity"))
            return {**host, "ready": alive and 0 <= age < 10, "alive": alive, "heartbeat_age_s": round(age, 3)}
        except (OSError, ValueError, KeyError) as error:
            return {"ready": False, "message": f"Cannot read host status: {error}"}

    def _public(self, job):
        result = dict(job)
        alive = process_alive(job.get("worker"))
        result["worker_alive"] = alive
        queued_host_alive = job["status"] == "queued" and not job.get("worker") and self.host_status()["ready"]
        if job["status"] not in TERMINAL and not alive and not queued_host_alive:
            result["status"] = "interrupted"
            result["error"] = "Worker is no longer alive; partial outputs are not valid results"
        result["cancellation_requested"] = (self.job_dir(job["job_id"]) / "cancel.request").exists()
        result["completed_runs"] = sum(run["status"] == "passed" for run in job["runs"])
        result["total_runs"] = len(job["runs"])
        result["elapsed_s"] = round((job.get("finished_epoch") or time.time()) - job["created_epoch"], 3)
        result["heartbeat_age_s"] = round(time.time() - job.get("heartbeat_epoch", job["created_epoch"]), 3)
        result["process_tree"] = []
        identity = job.get("engine")
        if process_alive(identity):
            try:
                parent = psutil.Process(identity["pid"])
                for proc in [parent, *parent.children(recursive=True)]:
                    try:
                        result["process_tree"].append({"pid": proc.pid, "create_time": proc.create_time(),
                                                       "name": proc.name()})
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        result["progress"] = None
        index = job.get("current_run")
        if index is not None and job["runs"][index].get("result_dir"):
            directory = self._result_path(job, job["runs"][index])
            log = tail_text(directory / "simulation.log", 100)
            result["progress"] = parse_progress(log["text"])
            if result["progress"]:
                result["progress"]["observed_log_age_s"] = round(time.time() - log["modified_epoch"], 3)
        if result["status"] == "passed":
            result["progress"] = {"simulation_time_s": 30.0, "simulation_limit_s": 30.0, "percent": 100.0}
        result["telemetry_note"] = "Progress is from flushed logs (normally every 5 wall-clock seconds); CBR/TX/RX CSV may be buffered."
        return result

    def start_run(self, schemes=None, seeds=None, request_id=None):
        schemes = ["G0"] if schemes is None else schemes
        seeds = [0] if seeds is None else seeds
        if not isinstance(schemes, list) or not schemes or any(type(x) is not str or x not in {"G0", "G1", "G2", "G3"} for x in schemes):
            raise ValueError("schemes must be a nonempty list containing G0, G1, G2 or G3")
        if not isinstance(seeds, list) or not seeds or any(type(x) is not int or not 0 <= x <= 2147483647 for x in seeds):
            raise ValueError("seeds must be a nonempty list of integers from 0 to 2147483647")
        if len(set(schemes)) != len(schemes) or len(set(seeds)) != len(seeds):
            raise ValueError("Duplicate schemes or seeds are not allowed")
        if len(schemes) * len(seeds) > 100:
            raise ValueError("At most 100 runs are allowed in one batch")
        if request_id is not None and (not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id)):
            raise ValueError("request_id must contain 1-64 letters, digits, underscores or hyphens")
        with start_lock(self.root):
            old = self._all_jobs()
            if request_id is not None:
                for job in old:
                    if job.get("request_id") == request_id:
                        if job["schemes"] != schemes or job["seeds"] != seeds:
                            raise ValueError("request_id already belongs to different parameters")
                        return self._public(job)
            for job in old:
                if self._public(job)["status"] not in TERMINAL:
                    raise RuntimeError("A simulation task is already active: " + job["job_id"])
            ready = self.settings.check()
            if not ready["ready"]:
                raise RuntimeError("Simulation environment is not ready: " + json.dumps(ready))
            host = self.host_status()
            if not host["ready"]:
                raise RuntimeError("Simulation host is not ready; run start_host.ps1 in a normal PowerShell session")
            job_id = "sim_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_") + uuid.uuid4().hex[:12]
            directory = self.root / "jobs" / job_id
            directory.mkdir(parents=True)
            now = time.time()
            job = {"job_id": job_id, "request_id": request_id, "status": "queued", "created_at": utc_now(),
                   "created_epoch": now, "heartbeat_epoch": now, "settings": self.settings.as_dict(),
                   "schemes": schemes, "seeds": seeds, "scope": ready["scope"], "current_run": None,
                   "runs": [{"index": i, "scheme": scheme, "seed": seed, "status": "pending", "result_dir": None}
                            for i, (seed, scheme) in enumerate((seed, scheme) for seed in seeds for scheme in schemes)]}
            save_json(directory / "job.json", job)
            return self._public(job)

    def get_status(self, job_id=None):
        if job_id is not None:
            return self._public(self._read(job_id))
        recent = self._all_jobs()[:10]
        return {"environment": self.settings.check(), "host": self.host_status(), "jobs": [self._public(job) for job in recent]}

    def _select_run(self, job, run_index):
        if run_index is None:
            run_index = job.get("current_run")
            if run_index is None:
                run_index = 0
        if type(run_index) is not int or not 0 <= run_index < len(job["runs"]):
            raise ValueError("run_index is outside this task (indices start at zero)")
        return job["runs"][run_index]

    def _result_path(self, job, run):
        root = (self.job_dir(job["job_id"]) / "project" / "results").resolve()
        path = Path(run["result_dir"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Result path is outside this task")
        return path

    def tail_log(self, job_id, log="simulation", lines=80, run_index=None):
        allowed = {"simulation": "simulation.log", "launcher": "launcher_console.log", "launchd": "launchd.log"}
        if log not in {*allowed, "worker", "driver"}:
            raise ValueError("log must be simulation, launcher, launchd, worker or driver")
        if type(lines) is not int or not 1 <= lines <= 500:
            raise ValueError("lines must be an integer between 1 and 500")
        job = self._read(job_id)
        run = self._select_run(job, run_index)
        directory = self.job_dir(job_id)
        if log == "worker":
            path = directory / "worker.log"
        elif log == "driver":
            path = directory / f"driver_{run['index']:03d}.log"
        elif run.get("result_dir"):
            path = self._result_path(job, run) / allowed[log]
        else:
            return {"job_id": job_id, "run_index": run["index"], "available": False, "text": "", "truncated": False}
        return {"job_id": job_id, "run_index": run["index"], "log": log, **tail_text(path, lines)}

    def stop_run(self, job_id):
        status = self.get_status(job_id)
        if status["status"] in TERMINAL:
            return status
        # Cooperative cancellation is interpreted by the owner, which kills its Job Object.
        (self.job_dir(job_id) / "cancel.request").touch(exist_ok=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = self.get_status(job_id)
            if status["status"] in TERMINAL:
                return status
            time.sleep(0.1)
        status["stop_pending"] = True
        status["message"] = "Cancellation requested; cleanup is still pending. Query get_status again."
        return status

    def get_results(self, job_id, run_index=None):
        job = self._read(job_id)
        status = self._public(job)
        selected = job["runs"] if run_index is None else [self._select_run(job, run_index)]
        results = []
        for run in selected:
            item = dict(run)
            item["complete"] = False
            item["summary"] = None
            item["files"] = []
            if run.get("result_dir"):
                directory = self._result_path(job, run)
                summary_path = directory / "summary.json"
                if summary_path.is_file():
                    try:
                        item["summary"] = read_json(summary_path)
                        summary = item["summary"]
                        if (not isinstance(summary, dict) or not isinstance(summary.get("checks"), list)
                                or any(not isinstance(check, dict) for check in summary["checks"])
                                or not isinstance(summary.get("metrics", {}), dict)):
                            raise ValueError("Malformed analysis summary structure")
                    except (OSError, ValueError) as error:
                        item["summary"] = None
                        item["summary_error"] = str(error)
                item["complete"] = (run["status"] == "passed" and isinstance(item["summary"], dict)
                                    and item["summary"].get("passed") is True
                                    and bool(item["summary"]["checks"])
                                    and all(check.get("passed") is True for check in item["summary"]["checks"]))
                if directory.is_dir():
                    item["files"] = [{"name": p.name, "size_bytes": p.stat().st_size} for p in sorted(directory.iterdir()) if p.is_file()]
                if run_index is None:
                    item["file_count"] = len(item.pop("files"))
                    summary = item["summary"]
                    if isinstance(summary, dict):
                        checks = summary.get("checks", [])
                        item["summary"] = {"passed": summary.get("passed"), "checks_total": len(checks),
                                           "failed_checks": [check for check in checks if not check.get("passed")],
                                           "events": summary.get("metrics", {}).get("events", []),
                                           "scope": summary.get("scope")}
            results.append(item)
        return {"job_id": job_id, "status": status["status"], "scope": job["scope"],
                "complete": status["status"] == "passed" and all(item["complete"] for item in results),
                "runs": results}
