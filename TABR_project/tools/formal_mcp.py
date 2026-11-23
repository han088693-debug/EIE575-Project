"""MCP for pre-registered TABR experiments. Run --start-host outside the MCP client.

The five tools submit and observe one registered scientific job per request. The
experiment CLI owns configuration validation, evidence rules and restart logic.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import traceback

import psutil

HERE = Path(__file__).resolve()
PROJECT = HERE.parents[1]
SETTINGS = json.loads((PROJECT / "configs/core.json").read_text(encoding="utf-8-sig"))["tools"]
sys.path.insert(0, SETTINGS["mcp_dir"])
from controller import read_json, save_json, start_lock, tail_text, utc_now
from process_control import OwnedProcess, process_alive

import experiment

# Batch root overrides are operator settings, never tool arguments.
BATCHES = Path(os.environ.get("EIE575_FORMAL_BATCH_ROOT", PROJECT / "results/batches")).resolve()
instance = hashlib.sha256(str(PROJECT).casefold().encode("utf-8")).hexdigest()[:12]
QUEUE = Path(os.environ.get("EIE575_FORMAL_QUEUE", Path(tempfile.gettempdir()) / ("tabr_formal_" + instance))).resolve()
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
END = {"completed", "failed", "cancelled", "interrupted"}


def identifier(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError("Use an alphanumeric identifier with optional hyphens or underscores")
    return value


def job_definition(batch_name, job_id):
    batch = (BATCHES / identifier(batch_name)).resolve()
    if batch.parent != BATCHES:
        raise ValueError("Batch is outside the configured batch root")
    batch_data = experiment.batch_read(batch)
    job = next((j for j in batch_data["jobs"] if j["job_id"] == identifier(job_id)), None)
    if job is None:
        raise ValueError("Job is not in the pre-registered matrix")
    return batch, batch_data, job


def task_file(token):
    return QUEUE / "requests" / identifier(token) / "state.json"


def host_state():
    path = QUEUE / "host.json"
    if not path.exists():
        return {"ready": False}
    value = read_json(path)
    return {**value, "ready": value.get("project") == str(PROJECT) and
            value.get("batch_root") == str(BATCHES) and process_alive(value.get("identity"))
            and 0 <= time.time() - value["heartbeat"] < 10}


def active_state(row):
    alive = process_alive(row.get("owner"))
    if row["state"] == "running" and not alive:
        return dict(row, state="interrupted", error="Owner exited; rerun the registered job with resume")
    return row


def start_run(batch: str, job: str, request_id: str, resume: bool = False) -> dict:
    """Enqueue one job from a registered batch. Retry with the same request_id to deduplicate."""
    _, registry, _ = job_definition(batch, job)
    return enqueue(request_id, dict(mode="batch", batch=batch, job=job, resume=resume, scope=registry["scope"]))


def probe_definition(probe):
    entries = read_json(PROJECT / "configs/formal_probes.json")["probes"]
    spec = entries[identifier(probe)]
    root = (PROJECT / "configs").resolve()
    cfg = (root / spec["config"]).resolve()
    if not cfg.is_relative_to(root) or cfg.suffix != ".json":
        raise ValueError("Probe configuration must be a registered project JSON file")
    config = read_json(cfg)
    if (spec["scope"] not in ("engineering", "pilot") or spec["scenario"] not in config["scenarios"]
            or spec["load_id"] not in config["loads"] or spec["scheme"] not in experiment.SCHEMES
            or type(spec["seed"]) is not int or spec["seed"] < 0 or spec["seed"] in config["seeds"]["formal"]
            or not math.isfinite(spec["density_per_lane_km"]) or spec["density_per_lane_km"] < 0):
        raise ValueError("Invalid registered probe configuration")
    if spec["scope"] == "pilot" and spec["seed"] not in config["seeds"]["pilot"]:
        raise ValueError("Unregistered pilot seed")
    return cfg, spec


def start_probe(probe: str, request_id: str) -> dict:
    """Submit an engineering or pilot entry from configs/formal_probes.json."""
    cfg, spec = probe_definition(probe)
    job = "{scenario}_{load_id}_{scheme}_seed{seed}".format(**spec)
    registration = hashlib.sha256(json.dumps(spec, sort_keys=True).encode() + cfg.read_bytes()).hexdigest()
    return enqueue(request_id, dict(mode="probe", probe=probe, job=job, scope=spec["scope"], registration=registration))


def probe_output(row):
    return BATCHES.parent / "probes" / identifier(row["probe"]) / identifier(row["request_id"])


def evidence_job(row):
    if row["mode"] == "batch":
        return job_definition(row["batch"], row["job"])
    dest = probe_output(row)
    b = experiment.batch_read(dest)
    return dest, b, next(j for j in b["jobs"] if j["job_id"] == row["job"])


def enqueue(request_id, parameters):
    path = task_file(request_id)
    with start_lock(QUEUE):
        if path.is_file():
            row = active_state(read_json(path))
            if any(row.get(k) != v for k, v in parameters.items()):
                raise ValueError("Request ID already used for other arguments")
            return row
        if not host_state()["ready"]:
            raise ValueError("Formal host is unavailable. Run tools/formal_mcp.py --start-host")
        for item in path.parent.parent.glob("*/state.json"):
            if active_state(read_json(item))["state"] not in END:
                raise ValueError("A formal request is active")
        path.parent.mkdir(parents=True, exist_ok=True)
        row = dict(request_id=request_id, state="queued", created_at=utc_now(), **parameters)
        save_json(path, row)
        return row


def get_status(request_id: str | None = None) -> dict:
    """Read the host heartbeat and either one request or the ten most recent requests."""
    if request_id is not None:
        row = active_state(read_json(task_file(request_id)))
        return {**row, "host": host_state(), "engine_alive": process_alive(row.get("engine"))}
    recent = sorted((QUEUE / "requests").glob("*/state.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {"host": host_state(), "batch_root": str(BATCHES), "queue": str(QUEUE),
            "requests": [active_state(read_json(p)) for p in recent[:10]]}


def tail_log(request_id: str, lines: int = 80, log: str = "driver") -> dict:
    """Read a bounded tail of an owned attempt's driver, engine or budget logs."""
    if type(lines) is not int or not 1 <= lines <= 500:
        raise ValueError("lines must be between 1 and 500")
    path = task_file(request_id)
    row = read_json(path)
    if log == "driver":
        return tail_text(path.with_name("driver.log"), lines)
    names = {"simulation": "simulation.log", "launcher": "launcher_console.log",
             "launchd": "launchd.log", "engine": "engine_status.json", "wall_budget": "wall_budget.jsonl"}
    if log not in names:
        raise ValueError("Select driver, simulation, launcher, launchd, engine or wall_budget")
    if row["mode"] == "probe" and not (probe_output(row) / "batch.json").is_file():
        return {"available": False, "text": ""}
    batch, b, j = evidence_job(row)
    attempts = [a for a in b["attempts"][j["job_id"]] if a not in row.get("prior_attempts", [])]
    token = row.get("attempt_id") or (attempts[-1] if attempts and row["state"] == "running" else None)
    if not token and row["state"] in END:
        token = get_results(request_id).get("attempt_id")
    if not token:
        return {"available": False, "text": ""}
    folder = experiment.attempt_dir(batch, b, j, token)
    for candidate in (folder / "logs" / names[log], folder / names[log]):
        if candidate.is_file():
            return tail_text(candidate, lines)
    return {"available": False, "text": ""}


def stop_run(request_id: str) -> dict:
    """Cancel this request. Its owner stops the corresponding Windows Job Object."""
    path = task_file(request_id)
    row = active_state(read_json(path))
    if row["state"] not in END:
        path.with_name("cancel.request").touch(exist_ok=True)
    return {**row, "cancellation_requested": path.with_name("cancel.request").exists()}


def get_results(request_id: str) -> dict:
    """Verify the selected recorded attempt and return its metrics and provenance."""
    row = active_state(read_json(task_file(request_id)))
    if row["state"] != "completed":
        result = {**row, "complete": False}
        if row["state"] not in END or not row.get("started_at"):
            return result
        try:
            batch, b, j = evidence_job(row)
            candidates = [a for a in b["attempts"][j["job_id"]] if a not in row.get("prior_attempts", [])]
            for token in candidates:
                folder = experiment.attempt_dir(batch, b, j, token)
                manifest = read_json(folder / "run_manifest.json")
                start = manifest.get("started_at", "")
                if start >= row["started_at"] and (not row.get("finished_at") or start <= row["finished_at"]):
                    result.update(attempt=str(folder), attempt_id=token, attempt_status=manifest.get("status"),
                                  attempt_error=manifest.get("error"))
                    break
        except (OSError, ValueError, KeyError, experiment.Failure):
            pass
        return result
    batch, b, j = evidence_job(row)
    token = row.get("attempt_id")
    if not token or token not in b["attempts"][j["job_id"]]:
        return {**row, "complete": False, "evidence_status": "missing_attempt"}
    folder = experiment.attempt_dir(batch, b, j, token)
    status, manifest = experiment.inspect_attempt(folder, b, j)
    if not manifest or status != "completed":
        return {**row, "complete": False, "evidence_status": status}
    return {**row, "complete": True, "attempt": str(folder), "attempt_id": manifest["attempt_id"],
            "simulation_job_key": manifest["simulation_job_key"], "metrics": read_json(folder / "metrics.json")}


def execute_gate(token):
    if sys.stdin.buffer.readline() != b"GO\n":
        return 20
    row = read_json(task_file(token))
    command = [SETTINGS["analysis_python"], "-B", "-u", "-X", "utf8", str(HERE.with_name("experiment.py"))]
    if row["mode"] == "probe":
        cfg, spec = probe_definition(row["probe"])
        registration = hashlib.sha256(json.dumps(spec, sort_keys=True).encode() + cfg.read_bytes()).hexdigest()
        if registration != row["registration"]:
            raise ValueError("Registered probe inputs changed after submission")
        command.extend(["probe", "--config", str(cfg), "--output", str(probe_output(row)),
                        "--scenario", spec["scenario"], "--load", spec["load_id"], "--scheme", spec["scheme"],
                        "--seed", str(spec["seed"]), "--density", str(spec["density_per_lane_km"])])
    else:
        batch, batch_data, job = job_definition(row["batch"], row["job"])
        registration = batch_data["physics_sha256"]
        command.extend(["run", "--batch", str(batch), "--job", job["job_id"]])
        if row["resume"]:
            command.append("--resume")
    env = dict(os.environ, EIE575_WALL_REQUEST=row["request_id"], EIE575_WALL_REGISTRATION=registration)
    return subprocess.call(command, cwd=PROJECT, env=env, creationflags=subprocess.CREATE_NO_WINDOW)


def serve():
    QUEUE.mkdir(parents=True, exist_ok=True)
    identity = {"pid": os.getpid(), "create_time": psutil.Process().create_time()}
    def heartbeat():
        save_json(QUEUE / "host.json", dict(project=str(PROJECT), batch_root=str(BATCHES),
                                           identity=identity, heartbeat=time.time()))
    with start_lock(QUEUE / "host_lease"):
        heartbeat()
        while True:
            row = None
            with start_lock(QUEUE):
                for path in sorted((QUEUE / "requests").glob("*/state.json")):
                    previous = read_json(path)
                    state = active_state(previous)
                    if state != previous:
                        save_json(path, state)
                    if state["state"] == "queued":
                        row = dict(state, state="running", owner=identity, started_at=utc_now())
                        save_json(path, row)
                        break
            if row is None:
                heartbeat()
                time.sleep(0.5)
                continue
            cancel = path.with_name("cancel.request")
            try:
                if cancel.exists():
                    row["state"] = "cancelled"
                else:
                    row["prior_attempts"] = []
                    if row["mode"] == "batch":
                        _, batch_data, job = evidence_job(row)
                        row["prior_attempts"] = list(batch_data["attempts"][job["job_id"]])
                    with path.with_name("driver.log").open("ab", buffering=0) as log:
                        with OwnedProcess([sys.executable, "-B", "-u", "-X", "utf8", str(HERE), "--gate",
                                           row["request_id"]], PROJECT, os.environ.copy(), log) as owned:
                            row["engine"] = owned.identity
                            save_json(path, row)
                            while owned.poll() is None:
                                heartbeat()
                                if cancel.exists():
                                    owned.stop()
                                    row["state"] = "cancelled"
                                    break
                                time.sleep(0.15)
                            row["exit_code"] = owned.process.wait()
                    if row["state"] != "cancelled":
                        row["state"] = "failed"
                        if row["exit_code"] == 0:
                            batch, batch_data, job = evidence_job(row)
                            state, _, record = experiment.selected(batch, batch_data, job)
                            if state != "completed" or record is None:
                                raise RuntimeError("CLI exited successfully without a complete requested attempt")
                            row.update(state="completed", attempt_id=record["attempt_id"])
            except Exception as exc:
                row.update(state="failed", error=str(exc))
                traceback.print_exc()
            finally:
                row["finished_at"] = utc_now()
                save_json(path, row)
                heartbeat()


def start_host():
    QUEUE.mkdir(parents=True, exist_ok=True)
    with start_lock(QUEUE / "launch_lease"):
        if not host_state()["ready"]:
            with (QUEUE / "host.log").open("ab", buffering=0) as log:
                p = subprocess.Popen([sys.executable, "-B", "-u", "-X", "utf8", str(HERE), "--serve"],
                                     cwd=PROJECT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                     creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
            for _ in range(100):
                if host_state()["ready"]:
                    break
                if p.poll() is not None:
                    raise RuntimeError("Formal host exited. Inspect " + str(QUEUE / "host.log"))
                time.sleep(0.1)
            else:
                raise RuntimeError("Formal host heartbeat unavailable")
    print(json.dumps(host_state()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    opts = parser.add_mutually_exclusive_group()
    opts.add_argument("--serve", action="store_true")
    opts.add_argument("--start-host", action="store_true")
    opts.add_argument("--gate", metavar="REQUEST")
    args = parser.parse_args()
    if args.gate:
        sys.exit(execute_gate(args.gate))
    if args.serve:
        serve()
    elif args.start_host:
        start_host()
    else:
        from mcp.server.fastmcp import FastMCP
        server = FastMCP("eie575-tabr-formal", log_level="WARNING")
        for tool in (start_run, start_probe, get_status, tail_log, stop_run, get_results):
            server.tool()(tool)
        server.run(transport="stdio")
