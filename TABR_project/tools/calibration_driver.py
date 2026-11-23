"""Recoverable P3 E0 control loop. The MCP host owns each engineering run."""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys
import time

import calibrate
import timeout_retry
import experiment
from experiment_config import load_config


PROJECT = Path(__file__).resolve().parents[1]
EXE = "EIE575_TABR_Starter.exe"
FAILURES = {"failed", "cancelled", "interrupted"}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def campaign_version(project, cfg):
    p2 = project / "verification/p2/acceptance.json"
    if not p2.is_file():
        return None, "P2 acceptance.json not yet available"
    receipt = experiment.json_read(p2)
    if receipt.get("phase") != "P2" or receipt.get("passed") is not True:
        return None, "P2 acceptance is incomplete"
    sources = experiment.simulation_sources(project)
    if not sources.get(EXE) or sources[EXE] != str(receipt.get("executable_sha256", "")).lower():
        return None, "P2 accepted executable differs from current executable"
    if cfg["stage"] != "candidate" or cfg.get("data_origin", "simulation") != "simulation" or cfg.get("synthetic"):
        raise ValueError("E0 requires a real candidate configuration")
    if cfg.get("resource_probe") or cfg.get("engineering_geometry"):
        raise ValueError("Calibration requires the full registered road and duration")
    runtime = experiment.runtime_hashes(cfg)
    key = experiment.identity({"p2": experiment.file_hash(p2), "design": calibrate.design_key(cfg),
                               "sources": sources, "runtime": runtime})
    return {"key": key, "p2_sha256": experiment.file_hash(p2), "design_sha256": calibrate.design_key(cfg),
            "sources": sources, "runtime": runtime}, None


def immutable(path, data):
    if path.is_file():
        require(experiment.json_read(path) == data, f"Existing registration differs: {path}")
    else:
        experiment.save(path, data)


def register(project, cfg, campaign):
    """Insert immutable MCP registrations without altering any P2 record."""
    tag = campaign["key"][:14]
    config_root = project / "configs"
    generated = config_root / "p3" / tag
    generated.mkdir(parents=True, exist_ok=True)
    probes = {}
    with experiment.locked(config_root / "p3"):
        registry = config_root / "formal_probes.json"
        data = experiment.json_read(registry)
        initial = copy.deepcopy(data)
        for warm in cfg["calibration"]["warmups_s"]:
            active = copy.deepcopy(cfg)
            active["timing"].update(warmup_s=warm, duration_s=warm + 100)
            active["loads"]["candidate"] = {"density_per_km_lane": None}
            active["observation_mode"] = "registered_pairs"
            path = generated / f"warmup{warm}.json"
            immutable(path, active)
            for density in cfg["calibration"]["initial_densities"] + cfg["calibration"]["additional_densities"]:
                for seed in cfg["seeds"]["calibration"]:
                    name = f"p3e0_{tag}_d{density}_w{warm}_s{seed}"
                    spec = {"scope": "engineering", "config": path.relative_to(config_root).as_posix(),
                            "scenario": "E0", "load_id": "candidate", "scheme": "G0", "seed": seed,
                            "density_per_lane_km": density}
                    require(name not in data["probes"] or data["probes"][name] == spec,
                            f"MCP registration conflict: {name}")
                    data["probes"][name] = spec
                    probes[(density, warm, seed)] = name
        if data != initial:
            experiment.save(registry, data)
    return probes


def protocol(cfg, tool, payload):
    """Use the installed official MCP ClientSession via the project's published client entry."""
    program = Path(cfg["tools"]["mcp_dir"]) / "client.py"
    process = subprocess.run([cfg["tools"]["mcp_python"], "-B", "-X", "utf8", str(program),
                              tool, "--args", json.dumps(payload), "--server", str(PROJECT / "tools/formal_mcp.py")],
                             capture_output=True, encoding="utf-8", errors="replace", timeout=150,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if process.returncode:
        raise RuntimeError(f"MCP {tool} error: {(process.stdout or process.stderr)[-1600:]}")
    reply = json.loads(process.stdout)
    if reply.get("isError"):
        raise RuntimeError(f"MCP {tool} error: {str(reply.get('content'))[:1200]}")
    value = reply.get("structuredContent")
    if value is not None:
        return value
    for block in reply.get("content", []):
        if block["type"] == "text":
            return json.loads(block["text"])
    raise RuntimeError(f"No structured MCP result for {tool}")


def verify_attempt(folder, job, receipt, version, deep):
    root = Path(folder)
    manifest_path = root / "run_manifest.json"
    manifest = experiment.json_read(manifest_path)
    require(manifest["sources"] == version["sources"], "Attempt simulation sources differ from campaign")
    require(manifest.get("status") == "completed" and manifest.get("data_valid") is True
            and manifest.get("scope") == "engineering", "Attempt lacks valid completed engineering status")
    require(manifest["attempt_id"] == receipt["attempt_id"] and manifest["simulation_job_key"] == receipt["simulation_job_key"],
            "MCP receipt does not identify this attempt")
    cfg = experiment.json_read(root / "config_snapshot.json")
    require(cfg.get("data_origin") == "simulation" and cfg["scheme"] == "G0" and cfg["scenario"] == "E0"
            and (cfg["density_per_lane_km"], cfg["timing"]["warmup_s"], cfg["seed"]) == tuple(job),
            "Completed attempt differs from the planned E0 request")
    require(calibrate.design_key(cfg) == version["design_sha256"], "Candidate design differs from campaign")
    if deep:
        for name, expected in manifest["files"].items():
            path = (root / name).resolve()
            require(path.is_relative_to(root.resolve()) and path.is_file() and experiment.file_hash(path) == expected,
                    f"Attempt file missing or changed: {name}")
    return experiment.file_hash(manifest_path)


def load_reports(output, state, version, deep=False):
    reports = []
    for row in state["requests"]:
        if row["state"] != "accepted":
            continue
        path = output / row["report"]
        require(path.is_file() and experiment.file_hash(path) == row["report_sha256"], f"Corrupted measurement: {path}")
        manifest_hash = verify_attempt(row["attempt"], row["job"], row, version, deep)
        require(manifest_hash == row["manifest_sha256"], "A previously indexed attempt manifest changed")
        reports.append(experiment.json_read(path))
    return reports


def preserve(output, state):
    if state["requests"] and state["requests"][-1]["state"] != "accepted":
        state["status"] = state["requests"][-1]["state"]
    experiment.save(output / "index.json", state)


def evaluate_finished(output, state, record, result, version):
    for key in ("attempt", "attempt_id", "simulation_job_key"):
        record[key] = result[key]
    if record.get("timeout_extension"):
        require(record["simulation_job_key"] == record["timeout_extension"]["parent_simulation_job_key"], "Increased time budget changed simulation job identity")
    record["manifest_sha256"] = verify_attempt(record["attempt"], record["job"], result, version, True)
    report = calibrate.evaluate(Path(record["attempt"]))
    path = output / "measurements" / f"{record['request_id']}.json"
    immutable(path, report)
    record.update(report=path.relative_to(output).as_posix(), report_sha256=experiment.file_hash(path), state="accepted")
    preserve(output, state)


def advance(project, cfg, output, state, version, probes, call, retry_failed=False, extend_timeout=None):
    """Advance by at most one submission or observation. Durable request_id precedes any MCP call."""
    def rpc(name, **kwargs):
        return call(cfg, name, kwargs)

    last = state["requests"][-1] if state["requests"] else None
    if last and last["state"] in {"submitted", "submitting", "queued", "running"}:
        if last["state"] == "submitting":
            # A previous submission may have reached the host before the client disconnected.
            reply = rpc("start_probe", probe=last["probe"], request_id=last["request_id"])
            require(reply["request_id"] == last["request_id"], "MCP returned an unrelated request")
            last["state"] = "submitted"
            preserve(output, state)
        reply = rpc("get_status", request_id=last["request_id"])
        require(reply["probe"] == last["probe"] and reply["request_id"] == last["request_id"], "MCP identity mismatch")
        phase = reply["state"]
        if phase in {"queued", "running"}:
            last["state"] = phase
            preserve(output, state)
            return {"status": phase, "current": last}, 40
        require(phase in FAILURES | {"completed"}, f"Unknown MCP state {phase}")
        result = rpc("get_results", request_id=last["request_id"])
        require(result["request_id"] == last["request_id"], "MCP evidence for other request")
        if phase != "completed" or not result.get("complete"):
            last.update(state="engineering_failed", mcp_state=phase, reason=result.get("attempt_error") or result.get("error")
                        or result.get("evidence_status") or phase, attempt=result.get("attempt"), attempt_id=result.get("attempt_id"))
            preserve(output, state)
            return {"status": last["state"], "current": last}, 20
        try:
            evaluate_finished(output, state, last, result, version)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            last.update(state="invalid_measurement", reason=str(exc))
            preserve(output, state)
            return {"status": last["state"], "current": last}, 30
    elif last and last["state"] in {"engineering_failed", "invalid_measurement"} and not retry_failed:
        return {"status": last["state"], "current": last}, 30 if last["state"] == "invalid_measurement" else 20

    geoms = sorted((project / "verification/p2/sumo").glob("geometry_exclusion_d*_seed*/inputs/scenario.json"))
    decision = calibrate.decide(cfg, load_reports(output, state, version), geometry_exclusions=geoms)
    experiment.save(output / "decision.json", decision)
    if decision["status"] == "selected":
        load_reports(output, state, version, deep=True)
        state["status"] = "selected"
        preserve(output, state)
        return {"status": "selected", "selected": decision["selected"], "warmup_s": decision["warmup_s"],
                "completed_unique_runs": decision["completed_unique_runs"], "next_phase": "E1"}, 0
    state["status"] = decision["status"]
    preserve(output, state)
    if not decision.get("next_runs"):
        return decision, 40
    first = decision["next_runs"][0]
    key = tuple(first[k] for k in ("density_per_lane_km", "warmup_s", "seed"))
    name = probes[key]
    name, extension = timeout_retry.select_probe(project, output, state, name, key, extend_timeout, immutable, preserve)
    # Both registered names belong to one scientific configuration and short request-ID sequence.
    count = sum(tuple(row["job"]) == key for row in state["requests"])
    # A short request directory keeps snapshot, SUMO and atomic-write paths within MAX_PATH.
    request_id = "e" + experiment.identity([version["key"], *key])[:14] + f"a{count + 1:02d}"
    entry = {"probe": name, "job": list(key), "request_id": request_id, "state": "submitting"}
    if extension:
        entry["timeout_extension"] = extension
    state["requests"].append(entry)
    preserve(output, state)
    reply = rpc("start_probe", probe=name, request_id=request_id)
    require(reply["request_id"] == request_id, "MCP returned an unrelated request")
    entry["state"] = "submitted"
    preserve(output, state)
    return {"status": "submitted", "current": entry, "completed_unique_runs": decision["completed_unique_runs"]}, 40


def run(config_path, output=None, advance_once=False, retry_failed=False, status_only=False, *, project=PROJECT, call=protocol, extend_timeout=None):
    require(extend_timeout is None or retry_failed, "--extend-timeout requires --retry-failed")
    cfg = load_config(Path(config_path))
    project = Path(project)
    version, blocker = campaign_version(project, cfg)
    if blocker:
        return {"status": "p2_not_accepted", "reason": blocker, "submitted": False}, 40
    directory = Path(output or project / "results/calibration" / version["key"][:14]).resolve()
    index = directory / "index.json"
    if status_only:
        if not index.is_file():
            return {"status": "ready", "output": str(directory), "submitted": False}, 40
        state = experiment.json_read(index)
        require(state["version"] == version, "Calibration source/runtime version changed")
        if state["status"] == "selected":
            load_reports(directory, state, version, deep=True)
        return {"status": state["status"], "output": str(directory),
                "current": state["requests"][-1] if state["requests"] else None}, 0 if state["status"] == "selected" else 40
    with experiment.locked(directory):
        probes = register(project, cfg, version)
        state = experiment.json_read(index) if index.is_file() else {"phase": "E0", "version": version, "requests": [], "status": "ready"}
        require(state["version"] == version, "Disjoint campaign needed after simulation sources/P2 receipt/config changed")
        load_reports(directory, state, version, deep=True)
        while True:
            live, reason = campaign_version(project, cfg)
            require(not reason and live == version, reason or "P2/configuration or simulator changed while running")
            result, code = advance(project, cfg, directory, state, version, probes, call, retry_failed, extend_timeout)
            result["output"] = str(directory)
            if advance_once or result["status"] not in {"submitted", "queued", "running"}:
                return result, code
            extend_timeout = None
            retry_failed = False
            time.sleep(8)


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--config", type=Path, required=True)
    args.add_argument("--output", type=Path)
    args.add_argument("--once", action="store_true", help="Submit/observe one job then yield")
    args.add_argument("--status", action="store_true", help="Read saved state without launching or modifying")
    args.add_argument("--retry-failed", action="store_true", help="Keep failed receipt and create the next attempt")
    args.add_argument("--extend-timeout", type=int, help="One larger seconds budget for a progressing timed-out E0 run")
    a = args.parse_args()
    try:
        result, code = run(a.config, a.output, a.once, a.retry_failed, a.status, extend_timeout=a.extend_timeout)
    except (ValueError, KeyError, OSError, RuntimeError, TypeError, subprocess.SubprocessError) as error:
        result, code = {"status": "driver_error", "message": str(error)}, 10
    print(json.dumps(result, ensure_ascii=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
