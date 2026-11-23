"""Recover evidence for an E0 wall budget masked by Windows' Cmdenv log-rename race."""
import csv
from datetime import datetime
from pathlib import Path
import json
import math


def last_complete_tick(path, field, run_id):
    with path.open("rb") as f:
        header=next(csv.reader([f.readline().decode("utf-8-sig")]))
        end=f.seek(0,2)
        start=max(0,end-128*1024)
        f.seek(start)
        if start:f.readline()
        chunk=f.read()
    if b"\n" not in chunk:return None
    # Ignore the trailing, possibly partial row left by forced process cleanup.
    chunk=chunk[:chunk.rfind(b"\n")+1].decode("utf-8")
    for record in reversed(chunk.splitlines()):
        values=next(csv.reader([record]))
        if len(values)==len(header) and values[header.index("run_id")]==run_id:
            return float(values[header.index(field)])
    return None


def classify(record, engine):
    """Return a derived view only; never write over the original engine_status or failed manifest."""
    if (record.get("state")!="engineering_failed" or engine.get("status")!="running" or
            engine.get("engine_complete") is not False or engine.get("return_code") is not None):
        return engine
    attempt=Path(record["attempt"]).resolve()
    logs=attempt/"logs"
    error=record.get("reason","")
    fixed_error=error.replace("\\\\","\\")
    if not ("[WinError 32]" in error and str(attempt/"inputs/.cmdenv-log") in fixed_error and
            str(logs/"cmdenv-internal.log") in fixed_error and (attempt/"inputs/.cmdenv-log").is_file()):
        return engine
    return paired_progress(record, engine, "masked_cmdenv_rename_at_wall_cap")


def buffered_timeout_progress(record, engine):
    """Use the same independent progress proof when a clean timeout left buffered console text."""
    if (record.get("state") != "engineering_failed" or engine.get("status") != "interrupted_timeout" or
            record.get("reason") != "Per-job wall timeout" or engine.get("error") != record["reason"] or
            engine.get("engine_complete") is not False or engine.get("return_code") is not None):
        return engine
    return paired_progress(record, engine, "buffered_cmdenv_at_confirmed_wall_cap")


def paired_progress(record, engine, evidence_reason):
    attempt = Path(record["attempt"]).resolve()
    logs = attempt / "logs"
    error = record["reason"]
    manifest=json.loads((attempt/"run_manifest.json").read_text(encoding="utf-8-sig"))
    config=json.loads((attempt/"config_snapshot.json").read_text(encoding="utf-8-sig"))
    if (manifest.get("status")!="engine_error" or manifest.get("error")!=error or
            manifest.get("attempt_id")!=record.get("attempt_id") or manifest.get("scope")!="engineering" or
            manifest.get("scenario")!="E0" or config.get("data_origin")!="simulation"):
        return engine
    cap=float(engine["wall_timeout_s"])
    elapsed=(datetime.fromisoformat(manifest["completed_at"])-datetime.fromisoformat(manifest["started_at"])).total_seconds()
    if not cap-1 <= elapsed <= cap+30: return engine
    samples=[]
    peak_mem=peak_handles=0
    last=None
    with (logs/"resource_trace.csv").open(encoding="utf-8-sig",newline="") as f:
        for row in csv.DictReader(f):
            wall=float(row["t_wall_s"])
            if int(row["process_count"])==0:continue
            peak_mem=max(peak_mem,int(row["private_bytes"]))
            peak_handles=max(peak_handles,int(row["handles"]))
            if row["t_sim_s"]:
                sim=float(row["t_sim_s"])
                if not samples or sim>samples[-1][1]:
                    samples.append((wall,sim,float(row["cpu_seconds"])))
            last=row
    if not last or not cap <= float(last["t_wall_s"]) <= cap+3 or len(samples)<3: return engine
    if int(last["process_count"])<2 or int(last["handles"])<=0 or not peak_mem:return engine
    cutoff=float(last["t_wall_s"])
    checkpoints={file:last_complete_tick(logs/file,col,manifest["run_id"]) for file,col in
                 (("control_log.csv","tick_time"),("state_log.csv","t"),("busy_audit.csv","transition_time"))}
    if any(v is None for v in checkpoints.values()):return engine
    physical=min(checkpoints.values())
    horizon=float(config["timing"]["duration_s"])
    if not samples[-1][1]<physical<horizon or float(last["cpu_seconds"])<=samples[-1][2]:
        return engine
    # The last two counter changes are time-stamped independently by the process monitor;
    # the last common physics tick was observed by wall cutoff (a conservative endpoint).
    progress=[(s,t) for t,s,_ in samples[-2:]]+[(physical,cutoff)]
    if not all(x[0]<y[0] and x[1]<y[1] for x,y in zip(progress,progress[1:])):return engine
    if any(not all(math.isfinite(z) for z in datum) for datum in progress):return engine
    # Process ownership verification is repeated immediately before authorizing any retry.
    return dict(engine,status="interrupted_timeout",error="Per-job wall timeout",peak_private_bytes=peak_mem,
                peak_handles=peak_handles,derived_timeout=dict(reason=evidence_reason,
                raw_status=engine["status"],raw_error=error,wall_interval_s=elapsed,final_sample=last,
                source_last_complete_ticks=checkpoints,wall_paired_physics=progress))


def check_ownership(engine, attempt):
    import psutil
    trace=engine.get("derived_timeout")
    if not trace:return
    port=engine["port"]
    if any(c.status==psutil.CONN_LISTEN and c.laddr.port==port for c in psutil.net_connections(kind="tcp")):
        raise ValueError("Previous SUMO launchd port still has a listener")
    # The final PID snapshot was masked. Check the current solver/launcher command and
    # SUMO working directory as well as the PID and birth-time checks of timeout_retry.
    owned_cmd=str(attempt.resolve()).casefold().replace("\\","/")
    for p in psutil.process_iter(["name"]):
        if p.info["name"].casefold() not in {"eie575_tabr_starter.exe","sumo.exe","python2.exe"}:continue
        try:
            cmd=" ".join(p.cmdline()).casefold().replace("\\","/")
            cwd=p.cwd().casefold().replace("\\","/")
            if owned_cmd in cmd or cwd.startswith(owned_cmd+"/"):
                raise ValueError(f"Old E0 process still alive: PID {p.pid}")
        except psutil.NoSuchProcess:pass
