"""P3 E1, pilots, paired-block recovery and freeze; execution requires a verified real E0."""

import argparse
import copy
import json
from pathlib import Path
import shutil
import sys
import time

import calibrate
import calibration_driver as e0
import experiment as ex
from experiment_config import load_config, validate_config
from p3_recovery import measured_budget, pack_block, recovery, require, verify_raw
from resource_probe import engine_evidence
from decimal import Decimal

PROJECT = Path(__file__).resolve().parents[1]


def load_selection(project, cfg, directory, deep):
    version, reason = e0.campaign_version(project, cfg)
    if reason:
        return None, reason
    if not (directory / "index.json").is_file() or not (directory / "decision.json").is_file():
        return None, "Missing E0 selection evidence"
    state = ex.json_read(directory / "index.json")
    if state.get("status") != "selected":
        return None, "E0 has not selected both loads"
    require(state["version"] == version, "P2, E0 sources, runtime or calibration design changed")
    reported = ex.json_read(directory / "decision.json")
    reports = e0.load_reports(directory, state, version, deep=deep)
    exclusions = sorted(
        (project / "verification/p2/sumo").glob("geometry_exclusion_d*_seed*/inputs/scenario.json")
    )
    decision = calibrate.decide(cfg, reports, geometry_exclusions=exclusions)
    require(
        decision == reported and decision["status"] == "selected" and decision.get("eligible_for_e1") is True,
        "E0 selection is not reproducible from real paired-seed observations",
    )
    spec = {
        "selected": decision["selected"],
        "warmup_s": decision["warmup_s"],
        "version": version,
        "e0_index_sha256": ex.file_hash(directory / "index.json"),
        "e0_decision_sha256": ex.file_hash(directory / "decision.json"),
    }
    require(0 < spec["selected"]["low"] < spec["selected"]["high"], "Unordered E0 selected loads")
    return spec, None


def active_config(cfg, selection):
    c = copy.deepcopy(cfg)
    c["observation_mode"] = "registered_pairs"
    c["timing"].update(warmup_s=selection["warmup_s"], duration_s=selection["warmup_s"] + 100)
    for load, density in selection["selected"].items():
        c["loads"][load]["density_per_km_lane"] = density
    validate_config(c)
    return c


def job(scenario, load, scheme, seed, role):
    return dict(
        job=f"{scenario}_{load}_{scheme}_seed{seed}",
        scenario=scenario,
        load_id=load,
        scheme=scheme,
        seed=seed,
        role=role,
    )


def scheduled(cfg):
    seed = cfg["seeds"]["acceptance"]
    pilots = cfg["seeds"]["pilot"]
    require(len(pilots) >= 2, "Two separate non-formal pilot seeds are required")
    ans = [job(s, "high", g, seed, "E1_high") for s in ("S0", "S1", "S2") for g in ex.SCHEMES]
    ans += [job(s, "low", "G0", seed, "low_window_check") for s in ("S0", "S1")]
    ans += [
        job("S1", load, g, k, "resource_pilot")
        for load, k in zip(("low", "high"), pilots)
        for g in ("G0", "G3")
    ]
    require(
        len({j["job"] for j in ans}) == 18 and not any(j["seed"] in cfg["seeds"]["formal"] for j in ans),
        "Engineering requests must be unique and disjoint from formal seeds",
    )
    return ans


def block_for_heaviest(rows):
    # Preserve the initially selected block; measure extra schemes solely to complete this block.
    measured = [r for r in rows if r["role"] != "complete_heaviest_block"]
    largest = max(measured, key=lambda r: (r["output_bytes"], r["job"]))
    return largest["scenario"], largest["load_id"], largest["seed"]


def probe_name(tag, j):
    return "v" + ex.identity([tag, j["job"]])[:10]


def native_path_budget(project, tag, j, attempt_number, cfg):
    # These two identifiers become path components twice removed from the original campaign.
    probe = probe_name(tag, j)
    request = "q" + ex.identity([tag, j["job"], attempt_number])[:12]
    destination = project / "results" / "probes" / probe / request
    token = "attempt-20000101T000000000000Z-" + "f" * 12
    path = ex.attempt_dir(
        destination, dict(scope="engineering", experiment_id=cfg["experiment_id"]), j, token
    )
    candidates = [
        path / "veins_runtime" / ("sumo-launchd-tmp-" + "f" * 8) / "sumo-launchd.err.log",
        path / "veins_runtime" / ("sumo-launchd-tmp-" + "f" * 8) / "sumo-launchd.out.log",
        path / "inputs" / "traffic.launchd.xml",
        path / "logs" / "sumo_diagnostics.json",
        path / "inputs" / "highway.net.xml",
        path / "inputs" / "traffic.sumo.cfg",
        path / "logs" / "launcher_console.log",
        path / "logs" / "coordinate_audit.csv",
        path / "logs" / "engine_counters.csv",
    ]
    longest = max((str(x.resolve()) for x in candidates), key=lambda p: len(p.encode("utf-16-le")))
    require(
        len(longest.encode("utf-16-le")) // 2 <= 259,
        "Windows path exceeds 259 UTF-16 units before submission: " + longest,
    )
    return request, longest


def register(project, tag, cfg, j):
    root = project / "configs"
    path = root / "p3" / tag / "engineering.json"
    name = probe_name(tag, j)
    spec = dict(
        scope="engineering",
        config=path.relative_to(root).as_posix(),
        scenario=j["scenario"],
        load_id=j["load_id"],
        scheme=j["scheme"],
        seed=j["seed"],
        density_per_lane_km=cfg["loads"][j["load_id"]]["density_per_km_lane"],
    )
    with ex.locked(root / "p3"):
        e0.immutable(path, cfg)
        registry = root / "formal_probes.json"
        r = ex.json_read(registry)
        if name in r["probes"]:
            require(r["probes"][name] == spec, "Changed probe registration")
        else:
            r["probes"][name] = spec
            ex.save(registry, r)
    return name


def validate_completed(j, reply, version, cfg):
    require(
        reply.get("state") == "completed"
        and reply.get("scope") == "engineering"
        and reply.get("complete") is True,
        "No completed real engineering result",
    )
    root = Path(reply["attempt"])
    mp = root / "run_manifest.json"
    mf = ex.file_hash(mp)
    m = verify_raw(root, mf)
    require(
        m["sources"] == version["sources"]
        and m["attempt_id"] == reply["attempt_id"]
        and m["simulation_job_key"] == reply["simulation_job_key"]
        and m["job_id"] == j["job"],
        "Returned source, run or job identity differs from registration",
    )
    snap = ex.json_read(root / "config_snapshot.json")
    expected = ex.job_snapshot(
        dict(cfg, density_per_lane_km=cfg["loads"][j["load_id"]]["density_per_km_lane"]), j, "engineering"
    )
    require(snap == expected, "Effective attempt configuration differs from its registration")
    metrics = ex.json_read(root / "metrics.json")
    require(
        metrics["data_origin"] == "simulation"
        and metrics["data_validity"] == "valid"
        and metrics["run_id"] == m["run_id"],
        "Analysis, origin or run identity is invalid",
    )
    require(
        len(metrics["events"]) == {"S0": 0, "S1": 1, "S2": 4}[j["scenario"]],
        "Unexpected registered primary event denominator",
    )
    trace = metrics["state"]["trajectory_sha256_by_vehicle"]
    require(trace and all(len(v) == 64 for v in trace.values()), "Missing independent trajectories")
    resources = engine_evidence(root, m["run_id"], Decimal(str(snap["timing"]["duration_s"])))
    traffic = ex.json_read(root / "logs/sumo_diagnostics.json")
    require(
        traffic["expected_entries"] == traffic["actual_entries"]
        and all(
            traffic[k] == 0 for k in ("insertion_failures", "unexplained_teleports", "unexplained_removals")
        ),
        "SUMO traffic departures and lifecycle do not reconcile",
    )
    return dict(
        **j,
        manifest_sha256=mf,
        attempt=str(root.resolve()),
        attempt_id=m["attempt_id"],
        simulation_job_key=m["simulation_job_key"],
        trajectory=ex.identity(trace),
        data_valid=True,
        bytes_in_attempt=sum(x.stat().st_size for x in root.rglob("*") if x.is_file()),
        output_bytes=int(m["output_bytes"]),
        wall_seconds=float(m["wall_seconds"]),
        private_bytes=resources["sampled_peak_private_bytes"],
        handles=resources["sampled_peak_handles"],
        streams=resources["exact_peak_project_log_streams"],
        events=len(metrics["events"]),
    )


def accepted(state, deep=False):
    rows = []
    by_block = {}
    for req in state["requests"]:
        if req.get("status") != "accepted":
            continue
        row = req["observation"]
        p = Path(row["attempt"])
        require(ex.file_hash(p / "run_manifest.json") == row["manifest_sha256"], "Accepted manifest changed")
        if deep:
            verify_raw(p, row["manifest_sha256"])
        group = (row["scenario"], row["load_id"], row["seed"])
        require(
            by_block.setdefault(group, row["trajectory"]) == row["trajectory"],
            "Different trajectories in a paired block",
        )
        rows.append(row)
    require(len(rows) == len({r["job"] for r in rows}), "Duplicate accepted job")
    return rows


def advance(project, dst, state, base, tag, call, *, retry=False, validate=validate_completed):
    index = dst / "index.json"

    def save():
        ex.save(index, state)

    regs = scheduled(base)
    rows = accepted(state)
    names = {r["job"] for r in rows}
    if len(rows) >= len(regs):
        sc, ld, k = block_for_heaviest(rows)
        standard = {r["job"] for r in regs}
        regs.extend(
            z
            for g in ex.SCHEMES
            if (z := job(sc, ld, g, k, "complete_heaviest_block"))["job"] not in standard
        )
    selected = next((r for r in regs if r["job"] not in names), None)
    if selected is None:
        state["status"] = "engineering_verified"
        save()
        return {"status": "engineering_verified", "accepted": len(rows)}, 0
    requests = state["requests"]
    last = requests[-1] if requests else None
    if last and last["job"] == selected["job"] and last["status"] in ("failed", "invalid") and not retry:
        return {"status": last["status"], "request": last}, 30 if last["status"] == "invalid" else 20
    if last is None or last["job"] != selected["job"] or last["status"] in ("failed", "invalid"):
        ident, longest = native_path_budget(project, tag, selected, len(requests) + 1, base)
        name = register(project, tag, base, selected)
        last = dict(
            job=selected["job"], request_id=ident, probe=name, status="submitting", max_native_path=longest
        )
        requests.append(last)
        save()

    def rpc(tool, **kw):
        reply = call(base, tool, kw)
        require(
            reply.get("request_id") == last["request_id"] and reply.get("probe") == last["probe"],
            "Unexpected MCP request identity",
        )
        return reply

    if last["status"] == "submitting":
        rpc("start_probe", probe=last["probe"], request_id=last["request_id"])
        last["status"] = "submitted"
        save()
    progress = rpc("get_status", request_id=last["request_id"])
    if progress["state"] in ("queued", "running"):
        last["status"] = progress["state"]
        save()
        return {"status": last["status"], "request_id": last["request_id"], "job": last["job"]}, 40
    reply = rpc("get_results", request_id=last["request_id"])
    receipt = dst / "mcp" / (last["request_id"] + ".json")
    ex.save(receipt, reply)
    last.update(receipt=str(receipt.relative_to(dst)), receipt_sha256=ex.file_hash(receipt))
    if not reply.get("complete"):
        last.update(
            status="failed",
            attempt=reply.get("attempt"),
            error=reply.get("attempt_error") or reply.get("state"),
        )
        save()
        return {"status": "failed", "request": last}, 20
    try:
        obs = validate(selected, reply, state["selection"]["version"], base)
        peers = [
            r
            for r in rows
            if (r["scenario"], r["load_id"], r["seed"]) == (obs["scenario"], obs["load_id"], obs["seed"])
        ]
        require(
            all(x["trajectory"] == obs["trajectory"] for x in peers),
            "Physics differ between paired engineering runs",
        )
    except (ValueError, KeyError, TypeError, OSError) as exc:
        last.update(status="invalid", attempt=reply.get("attempt"), error=str(exc))
        save()
        return {"status": "invalid", "request": last}, 30
    last.update(status="accepted", observation=obs)
    save()
    return {"status": "accepted", "accepted": len(rows) + 1, "job": selected["job"]}, 40


def freeze(project, dst, state, cfg, archive, recovery_report, free=None, frozen_path=None):
    from p3_recovery import recovery

    rows = accepted(state, deep=True)
    require(
        state["status"] in ("engineering_verified", "frozen")
        and {x["job"] for x in scheduled(cfg)}.issubset({r["job"] for r in rows}),
        "Engineering plan incomplete",
    )
    group = block_for_heaviest(rows)
    winners = [r for r in rows if (r["scenario"], r["load_id"], r["seed"]) == group]
    require(sorted(r["scheme"] for r in winners) == list(ex.SCHEMES), "Incomplete heaviest block")
    restored = Path(recovery_report["restored_to"])
    require(
        recovery_report["members"] == 4
        and recovery_report["archive_sha256"] == archive["archive_sha256"]
        and recovery_report["block_key"] == archive["block_key"]
        and len(recovery_report["library_file_ids"]) == len(archive["parts"]),
        "Restore and durable archive do not match",
    )
    for r in winners:
        verify_raw(restored / "attempts" / r["scheme"] / r["attempt_id"], r["manifest_sha256"])
    budget = measured_budget(rows)
    avail = shutil.disk_usage(project / "results").free if free is None else free
    report_path = dst / "resource_budget.json"
    if avail < budget["required_free_bytes"]:
        ex.save(
            dst / "space_blocker.json",
            dict(available_bytes=avail, required_bytes=budget["required_free_bytes"]),
        )
        return {
            "status": "insufficient_free_space",
            "available_bytes": avail,
            "required_bytes": budget["required_free_bytes"],
        }, 40
    # Pin the first passing observation so a crash before the final JSON writes can be resumed.
    if report_path.exists():
        previous = ex.json_read(report_path)
        require({k: previous[k] for k in budget} == budget, "Frozen resource evidence changed")
    else:
        ex.save(report_path, dict(budget, available_bytes=avail))
    frozen = copy.deepcopy(cfg)
    frozen["stage"] = "frozen"
    policy = {
        "wall_timeout_by_stratum_s": {k: v["timeout_s"] for k, v in budget["strata"].items()},
        "wall_timeout_s": max(v["timeout_s"] for v in budget["strata"].values()),
    }
    frozen["execution"] = dict(frozen.get("execution", {}), **policy)
    receipt = dst / "p3_evidence.json"
    evidence = dict(
        phase="P3",
        passed=True,
        freeze_physics_sha256=ex.freeze_physics_sha256(frozen),
        execution_policy_sha256=ex.identity(frozen["execution"]),
        e0=state["selection"],
        accepted=[
            {k: r[k] for k in ("job", "attempt_id", "manifest_sha256", "simulation_job_key", "trajectory")}
            for r in rows
        ],
        resource_budget_sha256=ex.file_hash(report_path),
        recovery=recovery_report,
        runtime_hashes=ex.runtime_hashes(cfg),
        simulation_sources=ex.simulation_sources(project),
    )
    e0.immutable(receipt, evidence)
    target = Path(frozen_path or project / "configs/frozen_core.json").resolve()
    import os

    frozen["p3_evidence"] = dict(path=os.path.relpath(receipt, target.parent), sha256=ex.file_hash(receipt))
    validate_config(frozen)
    ex.check_gate(frozen, target)
    e0.immutable(target, frozen)
    return {
        "status": "frozen",
        "config": str(target),
        "config_sha256": ex.file_hash(target),
        "receipt": str(receipt),
        "accepted": len(rows),
        "block": list(group),
    }, 0


def run(
    config_path,
    e0_dir,
    output,
    *,
    once=False,
    status=False,
    retry=False,
    restore_receipt=None,
    restore_dir=None,
    frozen_output=None,
    project=PROJECT,
    call=e0.protocol,
):
    project = project.resolve()
    cfg = load_config(config_path)
    selection, reason = load_selection(project, cfg, Path(e0_dir), False)
    if reason:
        return {"status": "awaiting_real_e0", "reason": reason, "submitted": False}, 40
    tag = ex.identity(selection)[:14]
    dst = Path(output or project / "results/p3_readiness" / tag).resolve()
    if status:
        if not (dst / "index.json").exists():
            return {"status": "ready", "planned": len(scheduled(cfg)), "submitted": False}, 40
        state = ex.json_read(dst / "index.json")
        require(state["selection"] == selection, "Changed E0 campaign")
        return {
            "status": state["status"],
            "accepted": len(accepted(state)),
            "requests": len(state["requests"]),
        }, (0 if state["status"] == "frozen" else 40)
    active = active_config(cfg, selection)
    with ex.locked(dst):
        index = dst / "index.json"
        if index.is_file():
            state = ex.json_read(index)
            require(state["selection"] == selection, "Changed gate; use a new output")
        else:
            selection, reason = load_selection(project, cfg, Path(e0_dir), True)
            require(selection and not reason, "Unverified E0")
            state = dict(format_version=1, status="engineering", selection=selection, requests=[])
            ex.save(index, state)
        if state["status"] == "frozen":
            result = state["frozen"]
            require(ex.file_hash(Path(result["config"])) == result["config_sha256"], "Frozen config changed")
            ex.check_gate(ex.json_read(Path(result["config"])), Path(result["config"]))
            return result, 0
        while state["status"] != "engineering_verified":
            output, code = advance(project, dst, state, active, tag, call, retry=retry)
            retry = False
            if code in (20, 30) or (once and output["status"] != "engineering_verified"):
                return output, code
            if output["status"] in ("queued", "running"):
                time.sleep(2)
        fresh, reason = load_selection(project, cfg, Path(e0_dir), True)
        require(fresh == selection and not reason, "E0 gate changed")
        rows = accepted(state, deep=True)
        group = block_for_heaviest(rows)
        block = [r for r in rows if (r["scenario"], r["load_id"], r["seed"]) == group]
        archive_index = pack_block(project, dst / "checkpoint", block, selection["version"], active)
        if not restore_receipt or not restore_dir:
            return {
                "status": "awaiting_durable_recovery",
                "upload_index": str(archive_index),
                "block": list(group),
            }, 40
        log = dst / "recovered.json"
        if log.exists():
            restored = ex.json_read(log)
            require(
                restored["recovery_receipt_sha256"] == ex.file_hash(Path(restore_receipt)),
                "Changed retrieval receipt",
            )
        else:
            restored = recovery(archive_index, Path(restore_receipt).resolve(), Path(restore_dir))
            e0.immutable(log, restored)
        result, code = freeze(
            project, dst, state, active, ex.json_read(archive_index), restored, frozen_path=frozen_output
        )
        if not code:
            state.update(status="frozen", frozen=result)
            ex.save(index, state)
        return result, code


def wait_for_e0(directory, interval, sleep=time.sleep):
    """Wait without submitting E0 work; existing E0 and P3 indexes allow restart."""
    if not 1 <= interval <= 60:
        raise ValueError("Polling interval must be between 1 and 60 seconds")
    previous = None
    while True:
        index = directory / "index.json"
        state = ex.json_read(index) if index.is_file() else {}
        latest = (state.get("requests") or [{}])[-1]
        phase = state.get("status", "awaiting_e0_index")
        if phase == "selected":
            return None
        if latest.get("state") in ("engineering_failed", "invalid_measurement"):
            return {
                "status": "e0_blocked",
                "reason": latest["state"],
                "request_id": latest.get("request_id"),
                "submitted": False,
            }, (30 if latest["state"] == "invalid_measurement" else 20)
        if phase in ("grid_exhausted", "common_warmup_failed", "geometry_evidence_required"):
            return {"status": "e0_blocked", "reason": phase, "submitted": False}, 40
        if phase != previous:
            print(
                json.dumps(dict(status="awaiting_real_e0", calibration_status=phase, submitted=False)),
                flush=True,
            )
            previous = phase
        sleep(interval)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("config", "e0-dir", "output", "restore-receipt", "restore-dir", "frozen-output"):
        p.add_argument("--" + key, type=Path, required=key in ("config", "e0-dir"))
    for key in ("once", "status", "retry"):
        p.add_argument("--" + key, action="store_true")
    p.add_argument(
        "--watch",
        action="store_true",
        help="Wait for the existing E0 campaign; then advance E1 until a gate or failure",
    )
    p.add_argument("--poll", type=float, default=15, help="Seconds between E0 status reads")
    a = p.parse_args()
    if a.watch and a.status:
        p.error("--status is read-only; choose --watch separately to authorize advancing E1")
    try:
        if a.watch:
            stop = wait_for_e0(a.e0_dir.resolve(), a.poll)
            if stop:
                result, code = stop
                print(json.dumps(result, ensure_ascii=True))
                return code
        result, code = run(
            a.config,
            a.e0_dir,
            a.output,
            once=a.once,
            status=a.status,
            retry=a.retry,
            restore_receipt=a.restore_receipt,
            restore_dir=a.restore_dir,
            frozen_output=a.frozen_output,
        )
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, ex.Failure) as exc:
        result, code = {"status": "error", "message": str(exc)}, getattr(exc, "code", 30)
    print(json.dumps(result, ensure_ascii=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
