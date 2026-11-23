"""One E0 wall-clock budget increase. Science inputs and timed-out attempt stay immutable."""
from collections import deque
from copy import deepcopy
import csv
import json
import math
from pathlib import Path
import re
import shutil

import experiment
import timeout_recovery

PROGRESS = re.compile(r"\*\* Event #(\d+)\s+t=([\d.eE+\-]+)\s+Elapsed:\s*([\d.eE+\-]+)s")


def need(condition, why):
    if not condition:
        raise ValueError(why)


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def timeout_status(record):
    if not record or not record.get("attempt"):
        return {}
    path = Path(record["attempt"]) / "logs/engine_status.json"
    return timeout_recovery.classify(record,load(path)) if path.is_file() else {}


def is_run_cap(status):
    return status.get("status") == "interrupted_timeout" and status.get("error") == "Per-job wall timeout"


def evidence(record, seconds):
    attempt = Path(record["attempt"])
    engine = timeout_status(record)
    need(is_run_cap(engine), "Only the simulator wall budget timeout qualifies")
    cap = float(engine["wall_timeout_s"])
    need(type(seconds) is int and math.isfinite(cap) and seconds > cap > 0, "New budget must exceed old cap")
    pids = engine["pids"]
    need(engine.get("port") and pids and all(i.get("pid") and i.get("create_time") for i in pids),
         "Need owned PID and creation-time evidence")
    import psutil
    for i in pids:
        try:
            proc = psutil.Process(i["pid"])
            need(abs(proc.create_time()-i["create_time"])>0.001, "A process owned by the old job is still running")
        except psutil.NoSuchProcess:
            pass
    timeout_recovery.check_ownership(engine,attempt)
    manifest = load(attempt / "run_manifest.json")
    need(manifest["attempt_id"] == record["attempt_id"] and manifest["scope"]=="engineering" and
         manifest["scenario"]=="E0", "Timeout is not the requested E0 attempt")
    timing = load(attempt / "config_snapshot.json")["timing"]
    samples = deque(maxlen=3)
    with (attempt/"logs/simulation.log").open(encoding="utf-8",errors="replace") as fh:
        for line in fh:
            hit = PROGRESS.search(line)
            if hit:
                samples.append((int(hit[1]),float(hit[2]),float(hit[3])))
    need(len(samples)>=2 and all(x[0]<y[0] and x[1]<y[1] and x[2]<y[2] for x,y in zip(samples,list(samples)[1:])),
         "Insufficient recent advancing Cmdenv event/time observations")
    # Parse the actual configured status cadence from recent messages. Ignore buffered
    # app counters for elapsed simulation time; they update only on inserts/removals.
    t0 = samples[0]; t1 = samples[-1]
    cadence = (t1[2]-t0[2])/(len(samples)-1)
    source = "Cmdenv event/time"
    derived = engine.get("derived_timeout")
    if not derived and cap-t1[2]>max(20,3*cadence):
        engine=timeout_recovery.buffered_timeout_progress(record,engine)
        derived=engine.get("derived_timeout")
        if derived:
            timeout_recovery.check_ownership(engine,attempt)
    if derived and cap-t1[2]>max(20,3*cadence):
        # Not OMNeT event numbers: the leading integer is an observation index.
        samples = [(i,sim,wall) for i,(sim,wall) in enumerate(derived["wall_paired_physics"])]
        t0,t1 = samples[0],samples[-1]
        source = "wall-stamped vehicle counters and independently logged final physical tick lower bound"
    else:
        need(cap - t1[2] <= max(20,3*cadence), "Last simulator progress is stale at the timeout")
    rate = (t1[1]-t0[1])/(t1[2]-t0[2])
    projected = t1[2] + (float(timing["duration_s"])-t1[1])/rate
    need(0<t1[1]<float(timing["duration_s"]), "Invalid or already completed simulator horizon")
    trace = attempt/"logs/resource_trace.csv"
    near = deque(maxlen=256)
    with trace.open(encoding="utf-8-sig",newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("process_count") and int(row["process_count"])>0:
                near.append(row)
    need(len(near)>=2 and float(near[-1]["t_wall_s"])>=cap-3, "Missing live resource trace at timeout")
    need(int(near[-1]["private_bytes"])>0 and int(near[-1]["handles"])>0, "Missing process resource observations")
    observed = sum(p.stat().st_size for p in attempt.rglob("*") if p.is_file())
    expected = math.ceil(observed * float(timing["duration_s"])/t1[1])
    free = shutil.disk_usage(attempt).free
    need(free >= 2*expected, "Insufficient disk for a fresh full attempt and its checkpoint")
    available_ram = psutil.virtual_memory().available
    return {"parent_attempt_id":record["attempt_id"], "parent_request":record["request_id"],
            "parent_simulation_job_key":manifest["simulation_job_key"], "old_wall_timeout_s":cap,
            "new_wall_timeout_s":seconds, "progress_source":source, "derived_timeout":derived, "progress_samples":list(samples), "projected_total_wall_s":projected,
            "peak_private_bytes":engine["peak_private_bytes"],"peak_handles":engine["peak_handles"],
            "last_private_bytes":int(near[-1]["private_bytes"]),"last_handles":int(near[-1]["handles"]),
            "free_bytes":free,"available_ram_bytes_after_cleanup":available_ram,
            "estimated_attempt_bytes":expected, "owned_processes":pids,
            "engine_status_sha256":experiment.file_hash(attempt/"logs/engine_status.json"),
            "trace_sha256":experiment.file_hash(trace),"progress_log_sha256":experiment.file_hash(attempt/"logs/simulation.log")}


def register_one(project, base, receipt, immutable):
    root=project/"configs"
    with experiment.locked(root/"p3"):
        registry=root/"formal_probes.json"
        data=load(registry)
        spec=data["probes"][base]; baseline=load(root/spec["config"])
        cfg=deepcopy(baseline)
        cfg.setdefault("execution",{})["wall_timeout_s"]=receipt["new_wall_timeout_s"]
        need(experiment.physics_fields(cfg)==experiment.physics_fields(baseline),"Budget retry changed scientific inputs")
        relative=Path(spec["config"]).parent/(base+"_ext1.json")
        immutable(root/relative,cfg)
        alias="x"+experiment.identity([base,"timeout-extension-1"])[:14]
        new_spec=dict(spec, config=relative.as_posix())
        need(alias not in data["probes"] or data["probes"][alias]==new_spec,"Conflicting previous extension")
        data["probes"][alias]=new_spec
        experiment.save(registry,data)
        return alias


def select_probe(project, output, state, base, key, seconds, immutable, preserve):
    previous=[r for r in state["requests"] if tuple(r["job"])==tuple(key)]
    last=previous[-1] if previous else None
    extensions=state.setdefault("timeout_extensions",{})
    saved=extensions.get(base)
    if saved and any(r["probe"]==saved["probe"] for r in previous):
        raise ValueError("One increased-budget attempt already submitted for this E0 configuration")
    if not is_run_cap(timeout_status(last)):
        need(seconds is None,"The selected attempt was not interrupted by its simulation wall-clock budget")
        return base,None
    if saved:
        need(saved["parent_attempt_id"]==last["attempt_id"],"Extension belongs to a different attempt")
        need(seconds in (None,saved["new_wall_timeout_s"]),"Previously recorded budget differs")
        return saved["probe"],saved
    need(seconds is not None,"Specify --extend-timeout SECONDS with --retry-failed after reviewing resource health")
    path=output/"timeout_extensions"/(base+".json")
    if path.is_file():
        receipt=load(path)
        need(receipt["parent_attempt_id"]==last["attempt_id"] and receipt["new_wall_timeout_s"]==seconds,
             "Conflicting extension receipt")
    else:
        receipt=evidence(last,seconds)
        immutable(path,receipt)
    alias=register_one(project,base,receipt,immutable)
    saved=dict(probe=alias,parent_attempt_id=last["attempt_id"],parent_request=last["request_id"],
               parent_simulation_job_key=receipt["parent_simulation_job_key"],new_wall_timeout_s=seconds,
               receipt_sha256=experiment.file_hash(path),receipt=path.relative_to(output).as_posix())
    extensions[base]=saved
    preserve(output,state)
    return alias,saved
