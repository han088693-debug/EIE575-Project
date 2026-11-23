"""Passive E0 watcher; take over only a verified, progressing simulation wall timeout."""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time

import calibration_driver
import experiment
import timeout_retry


END = {"selected", "grid_exhausted", "common_warmup_failed", "geometry_evidence_required",
       "partial_calibration", "invalid_measurement", "mixed_design"}
ACTIVE = {"submitting", "submitted", "queued", "running"}


def auto_budget(record):
    """A fresh attempt reruns from t=0, so budget the entire run, not just its remainder."""
    status = timeout_retry.timeout_status(record)
    old = float(status["wall_timeout_s"])
    # This first pass only reads evidence and confirms progress/ownership/space.
    measured = timeout_retry.evidence(record, math.ceil(old)+1)
    final = measured["progress_samples"][-1]
    snapshot = experiment.json_read(Path(record["attempt"])/"config_snapshot.json")
    if snapshot.get("data_origin") != "simulation":
        raise ValueError("E0 timeout continuation requires real simulation provenance")
    duration = float(snapshot["timing"]["duration_s"])
    average = final[2] * duration / final[1]
    projected = max(average, measured["projected_total_wall_s"], old)
    budget = 60 * math.ceil((projected * 1.25+60)/60)
    return {"new_wall_timeout_s":budget, "old_wall_timeout_s":old, "recent_rate_total_wall_s":measured["projected_total_wall_s"],
            "whole_run_rate_total_wall_s":average, "margin_fraction":0.25, "fixed_margin_s":60,
            "round_up_s":60, "parent_attempt_id":record["attempt_id"], "parent_request":record["request_id"],
            "engine_status_sha256":measured["engine_status_sha256"]}


def busy(error):
    return isinstance(error, experiment.Failure) and error.code == 40 and "Batch is in use" in str(error)


def save_monitor(path, state, status, reason=None, **new):
    new_state = dict(state, **new, status=status)
    if reason:
        new_state["reason"] = reason
    else:
        new_state.pop("reason",None)
    comparable = {k:v for k,v in state.items() if k!="updated_at"}
    if comparable != {k:v for k,v in new_state.items() if k!="updated_at"}:
        new_state["updated_at"]=datetime.now(timezone.utc).isoformat()
        experiment.save(path,new_state)
    return new_state


def advance_if_current(config, output, *, expected_request, expected_campaign, retry_failed=False, extend_timeout=None):
    """Run the stock driver's one transition only if the same request still heads the queue."""
    with experiment.locked(output):
        state = experiment.json_read(output/"index.json")
        last = state["requests"][-1] if state["requests"] else {}
        if (state["version"]["key"] != expected_campaign or
                (last.get("request_id"), last.get("state"), last.get("attempt_id")) != expected_request):
            return {"status":"controller_advanced"},40
        cfg = calibration_driver.load_config(config)
        version,reason = calibration_driver.campaign_version(calibration_driver.PROJECT,cfg)
        if reason or version != state["version"]:
            raise ValueError(reason or "E0 campaign evidence or simulation version changed")
        probes=calibration_driver.register(calibration_driver.PROJECT,cfg,version)
        return calibration_driver.advance(calibration_driver.PROJECT,cfg,output,state,version,probes,
                                        calibration_driver.protocol,retry_failed,extend_timeout)


def cycle(config, output, monitor_path, *, driver=advance_if_current, make_budget=auto_budget):
    monitor = experiment.json_read(monitor_path) if monitor_path.is_file() else {}
    try:
        # The stock driver holds this OS lock while monitoring or submitting. It
        # reacquires the same lock inside run(), so a racing restart cannot duplicate work.
        with experiment.locked(output):
            state = experiment.json_read(output/"index.json")
    except experiment.Failure as exc:
        if not busy(exc):
            raise
        info = save_monitor(monitor_path, monitor, "observing_active_controller")
        return info, False, 0
    version = state["version"]["key"]
    if monitor.get("campaign_key",version)!=version:
        return save_monitor(monitor_path,monitor,"stopped","campaign identity changed"),True,40
    last = state["requests"][-1] if state.get("requests") else {}
    if (monitor.get("status")=="stopped" and monitor.get("stopped_request")==last.get("request_id")
            and monitor.get("stopped_campaign_status")==state["status"] and not (
                last.get("state")=="engineering_failed" and "[WinError 32]" in monitor.get("reason","") and
                timeout_retry.timeout_status(last).get("derived_timeout"))):
        return monitor,True,40
    monitor.update(campaign_key=version)
    def stop(reason,code=40):
        return save_monitor(monitor_path,monitor,"stopped",reason,stopped_request=last.get("request_id"),
                            stopped_campaign_status=state["status"]),True,code
    status = state["status"]
    if status == "selected":
        return save_monitor(monitor_path,monitor,"selected","E0 decision and traces saved"),True,0
    if status in END:
        return stop(last.get("reason") or status)
    action = {}
    if last.get("state") == "engineering_failed":
        source = timeout_retry.timeout_status(last)
        if not timeout_retry.is_run_cap(source):
            return stop(last.get("reason") or "engineering failure other than simulator wall timeout",20)
        if last.get("timeout_extension") or any(
                r.get("timeout_extension") and r["job"]==last["job"] for r in state["requests"]):
            return stop("increased-budget attempt already used for this E0 configuration",20)
        saved = monitor.get("budget_decisions",{}).get(last["attempt_id"])
        try:
            if saved is None:
                saved=make_budget(last)
            elif saved["engine_status_sha256"]!=experiment.file_hash(Path(last["attempt"])/"logs/engine_status.json"):
                return stop("recorded timeout evidence changed")
        except (ValueError,KeyError,RuntimeError,OSError) as exc:
            return stop("timeout did not pass continuation evidence checks: "+str(exc),20)
        # Persist intent before submitting. A restart reuses this exact budget
        # and the stock driver's already-persisted idempotent request ID.
        decisions=dict(monitor.get("budget_decisions",{}))
        decisions[last["attempt_id"]]=saved
        monitor=save_monitor(monitor_path,monitor,"extension_recorded",budget_decisions=decisions,owns_continuation=True)
        action={"retry_failed":True,"extend_timeout":saved["new_wall_timeout_s"]}
    elif not monitor.get("owns_continuation") and last.get("state") not in {"submitted","queued","running"}:
        return stop("controller inactive without a confirmed simulator wall timeout; no job submitted")
    elif status in {"invalid_measurement","engineering_failed"}:
        return stop(last.get("reason") or status)
    try:
        # The original driver retains queue ownership and deduplication. One step
        # returns promptly; the watcher can record its state before the next poll.
        result, code = driver(config,output,expected_request=(last.get("request_id"),last.get("state"),last.get("attempt_id")),
                              expected_campaign=version,**action)
    except experiment.Failure as exc:
        if busy(exc):
            return save_monitor(monitor_path,monitor,"observing_active_controller"),False,0
        return stop(str(exc),exc.code)
    except Exception as exc:
        return stop(str(exc),20)
    phase=result["status"]
    if phase=="controller_advanced":
        return save_monitor(monitor_path,monitor,"observing_active_controller"),False,0
    child=(result.get("current") or {}).get("request_id")
    monitor=save_monitor(monitor_path,monitor,"following_driver",driver_status=phase,current_request=child)
    if phase=="selected":
        return save_monitor(monitor_path,monitor,"selected"),True,0
    if phase=="engineering_failed":
        # The durable index now contains the real exit/reason. The next cycle
        # can decide whether this is a distinct eligible configuration.
        return monitor,False,0
    if phase not in ACTIVE:
        return stop(result.get("message") or phase,code or 40)
    return monitor,False,0


def bind_readiness(path, pid, birth, log, config, output):
    if path.is_file():
        data=experiment.json_read(path)
        if pid is not None and (data["pid"]!=pid or abs(data["create_time"]-birth)>.001):
            raise ValueError("A different readiness process is already bound")
        return data
    if pid is None:
        return None
    import psutil
    p=psutil.Process(pid)
    if abs(p.create_time()-birth)>.001:
        raise ValueError("Readiness PID has been reused")
    cwd=Path(p.cwd()).resolve()
    argv=p.cmdline()
    script=calibration_driver.PROJECT/"tools/p3_readiness.py"
    positions=[i for i,arg in enumerate(argv) if arg.endswith("p3_readiness.py") and (cwd/arg).resolve()==script]
    if len(positions)!=1 or "--watch" not in argv or any(v in argv for v in ("--status","--retry","--once")):
        raise ValueError("Only the standard continuous E0-gated readiness watcher can be bound")
    options={};args=iter(argv[positions[0]+1:])
    paths={"--config","--e0-dir","--output","--restore-receipt","--restore-dir","--frozen-output"}
    for key in args:
        if key=="--watch":continue
        if key=="--poll":next(args);continue
        if key not in paths:raise ValueError(f"Unsupported bound readiness argument: {key}")
        options[key]=str((cwd/next(args)).resolve())
    if options.get("--config")!=str(config) or options.get("--e0-dir")!=str(output):
        raise ValueError("Bound readiness watcher belongs to a different config or E0 campaign")
    data={"pid":pid,"create_time":birth,"options":options,"log":str(Path(log).resolve())}
    experiment.save(path,data)
    return data


def handoff(folder,binding,monitor,*,live=None,drive=None):
    from p3_readiness import run as drive_readiness
    import psutil
    def matches():
        try:
            return abs(psutil.Process(binding["pid"]).create_time()-binding["create_time"])<=.001
        except psutil.NoSuchProcess:
            return False
    if not binding:
        return monitor,0
    receipt=folder/"readiness_handoff.json"
    previous=experiment.json_read(receipt) if receipt.is_file() else {}
    if previous.get("status") in ("readiness_returned","handoff_stopped"):
        return previous,previous["code"]
    if (live or matches)():
        answer={"status":"original_readiness_active","pid":binding["pid"],"code":0}
        experiment.save(receipt,answer)
        return answer,0
    # Restart only a watcher which explicitly exited at the same extended E0
    # failure. An E1 failure or an intentional stop must not trigger a second job.
    latest={}
    with Path(binding["log"]).open(encoding="utf-8-sig",errors="replace") as f:
        for line in f:
            try:value=json.loads(line)
            except ValueError:continue
            if isinstance(value,dict):latest=value
    parents={r["parent_request"] for r in monitor.get("budget_decisions",{}).values()}
    if (latest.get("status")!="e0_blocked" or latest.get("reason")!="engineering_failed" or
            latest.get("request_id") not in parents):
        answer={"status":"handoff_stopped","reason":"original watcher did not exit at the extended E0 failure", "code":40}
        experiment.save(receipt,answer)
        return answer,40
    opts={k[2:].replace("-","_"):Path(v) for k,v in binding["options"].items()}
    config=opts.pop("config");e0=opts.pop("e0_dir");output=opts.pop("output",None)
    experiment.save(receipt,{"status":"resuming_bound_readiness","parent_request":latest["request_id"]})
    try:
        result,code=(drive or drive_readiness)(config,e0,output,**opts)
    except experiment.Failure as exc:
        if busy(exc):
            answer={"status":"readiness_already_owned","code":0};experiment.save(receipt,answer);return answer,0
        result,code={"status":"handoff_error","reason":str(exc)},exc.code
    except Exception as exc:
        result,code={"status":"handoff_error","reason":str(exc)},10
    answer={"status":"readiness_returned","result":result,"code":code}
    experiment.save(receipt,answer)
    return answer,code


def watch(config,output,poll=15,once=False,readiness_pid=None,readiness_birth=None,readiness_log=None):
    config,output=Path(config).resolve(),Path(output).resolve()
    if not (output/"index.json").is_file():
        raise ValueError("Specify an existing E0 campaign with index.json")
    if not math.isfinite(poll) or poll<=0:
        raise ValueError("Positive polling interval required")
    folder=output/"timeout_monitor"
    with experiment.locked(folder):
        binding=bind_readiness(folder/"readiness_binding.json",readiness_pid,readiness_birth,readiness_log,config,output)
        while True:
            result,done,code=cycle(config,output,folder/"status.json")
            if done and result["status"]=="selected" and binding:
                return handoff(folder,binding,result)
            if done or once:
                return result,code
            time.sleep(poll)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True,help="Existing E0 campaign directory")
    parser.add_argument("--poll",type=float,default=15)
    parser.add_argument("--once",action="store_true",help="Observe or advance at most once, then exit")
    parser.add_argument("--readiness-pid",type=int)
    parser.add_argument("--readiness-create-time",type=float)
    parser.add_argument("--readiness-log",type=Path,help="Existing bound watcher's stdout log")
    args=parser.parse_args()
    if len({v is None for v in (args.readiness_pid,args.readiness_create_time,args.readiness_log)})>1:
        parser.error("provide all three readiness binding arguments together")
    try:
        result,code=watch(args.config,args.output,args.poll,args.once,args.readiness_pid,args.readiness_create_time,args.readiness_log)
    except (OSError,ValueError,KeyError,experiment.Failure) as exc:
        result,code={"status":"monitor_error","reason":str(exc)},getattr(exc,"code",10)
    print(json.dumps(result,ensure_ascii=True))
    return code


if __name__=="__main__":
    sys.exit(main())
