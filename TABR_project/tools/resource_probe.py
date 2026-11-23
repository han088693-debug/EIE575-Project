"""Audit short resource runs and compare a complete full/trimmed logging pair."""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import shutil
import sys
import tempfile
from terminal_coverage import check_final_control, completed_trips

from formal_analyze import analyze_attempt as formal_metrics, audit_transport, read_table
from experiment import input_fingerprint, physics_fields


SCALES = (16, 64, 160, 480, 960)


def need(ok, message):
    if not ok:
        raise ValueError(message)


def doc(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def rows(path, fields):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        need(fields <= set(reader.fieldnames or []), f"{path.name}: required header missing")
        for row in reader:
            need(None not in row and None not in row.values(), f"{path.name}:{reader.line_num}: incomplete row")
            yield row


def quantity(value):
    x = Decimal(value)
    need(x.is_finite(), "Nonfinite logged quantity")
    return x


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def finish_check(root, manifest):
    need(manifest.get("status") == "completed" and manifest.get("data_valid") is True, "Attempt not completed/valid")
    evidence = manifest.get("files", {})
    need("metrics.json" in evidence and "logs/engine_status.json" in evidence, "Incomplete attempt file index")
    for relative, expected in evidence.items():
        path = (root / relative).resolve()
        need(path.is_relative_to(root.resolve()) and path.is_file() and sha(path) == expected,
             f"File checksum mismatch: {relative}")


def vehicle_counts(root, rid, duration):
    life, changes = {}, defaultdict(lambda: Counter())
    target = {p[k] for p in read_table(root, "pairs", rid) if p["role"] == "target" for k in ("sender_id", "receiver_id")}
    for row in read_table(root, "lifecycle", rid):
        car, start, end = row["vehicle_id"], quantity(row["enter_s"]), quantity(row["exit_s"])
        tx, rx = int(row["tx_count"]), int(row["rx_count"])
        need(car not in life and 0 <= start <= end <= duration and min(tx, rx) >= 0, f"{car}: invalid lifecycle")
        life[car] = (start, end, tx, rx)
        if start < end:
            changes[start]["all"] += 1
            changes[end]["all"] -= 1
            if car not in target:
                changes[start]["background"] += 1
                changes[end]["background"] -= 1
            changes[start]["entries"] += 1
    need(target and target <= life.keys(), "Target is absent from the lifecycle")
    need(all(life[car][1] == duration for car in target), "Target lifetimes do not reach the planned end")
    count, background, max_count, max_bg, transient_bound = 0, 0, 0, 0, 0
    for _, d in sorted(changes.items()):
        # Entry and departure callbacks may be interleaved at the same simulated instant.
        transient_bound = max(transient_bound, count + d["entries"])
        count += d["all"]
        background += d["background"]
        max_count, max_bg = max(max_count, count), max(max_bg, background)
    need(count == background == 0, "Lifecycle end counts do not reconcile")
    return life, max_count, max_bg, transient_bound


def engine_evidence(root, rid, duration):
    logs = root / "logs"
    status = doc(logs / "engine_status.json")
    need(status.get("engine_complete") is True and status.get("return_code") == 0, "Engine incomplete or nonzero exit")
    need(status.get("pids") and float(status["wall_seconds"]) > 0, "Missing owned process or timing evidence")
    pmax = hmax = samples = 0
    first_wall = last_wall = None
    max_gap = 0.0
    for r in rows(logs / "resource_trace.csv", {"t_wall_s", "private_bytes", "handles", "process_count"}):
        wall, mem, handles, processes = float(r["t_wall_s"]), int(r["private_bytes"]), int(r["handles"]), int(r["process_count"])
        need(math.isfinite(wall) and wall >= 0 and min(mem, handles, processes) >= 0, "Invalid process resource sample")
        if first_wall is None:
            first_wall = wall
        if last_wall is not None:
            need(wall >= last_wall, "Resource trace out of order")
            max_gap = max(max_gap, wall - last_wall)
        last_wall = wall
        samples += 1
        pmax, hmax = max(pmax, mem), max(hmax, handles)
    need(samples > 0 and pmax > 0 and hmax > 0, "No live process-tree samples")
    need(status["peak_private_bytes"] >= pmax and status["peak_handles"] >= hmax, "Resource summary below logged samples")
    streams = active = count = 0
    prev = Decimal("-1")
    for r in rows(logs / "engine_counters.csv", {"t_sim_s", "open_log_streams", "peak_open_log_streams", "active_vehicle_count"}):
        t = quantity(r["t_sim_s"])
        opened, peak, vehicles = (int(r[k]) for k in ("open_log_streams", "peak_open_log_streams", "active_vehicle_count"))
        need(prev <= t <= duration and 0 <= opened <= peak and streams <= peak and vehicles >= 0,
             "Invalid engine counter sample")
        prev, streams, active = t, peak, max(active, vehicles)
        count += 1
    need(count > 0 and 0 < streams <= 32, f"Project log stream high-water is {streams} (expected 1–32)")
    return {"engine_complete": True, "return_code": status["return_code"], "wall_seconds": status["wall_seconds"],
            "process_tree_samples": samples, "first_sample_wall_s": first_wall, "last_sample_wall_s": last_wall,
            "max_sample_gap_s": max_gap, "sampled_peak_private_bytes": status["peak_private_bytes"],
            "sampled_peak_handles": status["peak_handles"], "exact_peak_project_log_streams": streams,
            "peak_active_vehicle_callbacks": active, "counter_rows": count,
            "log_output_bytes": sum(p.stat().st_size for p in logs.rglob("*") if p.is_file())}


def audit_short(root, cfg, m, duration, life):
    from formal_analyze import HEADERS, INPUTS
    from formal_state import Clock, BusyAudit, load_lifecycle, load_truth
    rid = m["run_id"]
    transport = audit_transport(root, cfg, m)
    # Fixed-tick coverage is determined by each actual lifetime, including zero complete windows.
    step = quantity(str(cfg["timing"]["control_step_s"]))
    eps = quantity(str(cfg["timing"]["time_resolution_s"]))
    clock = Clock(cfg["timing"])
    by_tick = load_lifecycle(root / "logs", clock.tick, clock.tick(duration))
    mac = BusyAudit(root / "logs/busy_audit.csv", by_tick, clock.tick)
    max_busy_error = 0.0
    last = {v: lo for v, (lo, hi, _, _) in life.items()}
    count = 0
    for row in read_table(root, "control_log", rid):
        v = row["vehicle_id"]
        lo, hi, tick = (quantity(row[k]) for k in ("window_start", "window_end", "tick_time"))
        need(v in life and abs(lo - last[v]) <= eps and abs(hi - lo - step) <= eps and abs(tick-hi) <= eps
             and hi <= life[v][1] + eps, f"Non-contiguous control windows for {v}")
        need(0 <= quantity(row["C"]) <= 1 and 0 <= quantity(row["Cbar"]) <= 1, "Invalid CBR range")
        mac.advance(clock.tick(hi))
        if by_tick[v][4]:
            recomputed = mac.integral(v, clock.tick(hi)) * clock.sec
            discrepancy = abs(recomputed - float(row["busy_duration_s"]))
            need(discrepancy <= cfg["analysis"]["busy_integral_tolerance_s"], "Short-run independent busy audit failed")
            max_busy_error = max(max_busy_error, discrepancy)
        last[v], count = hi, count + 1
    traffic = doc(root / "logs/sumo_diagnostics.json")
    arrivals = completed_trips(root, traffic)
    motion_step = quantity(str(cfg["timing"]["motion_step_s"]))
    removed_before_tick = [v for v, (_, end, _, _) in life.items()
                           if check_final_control(v, end, last[v], step, motion_step, eps, arrivals)]
    need(count > 0, "No fixed-tick observations")
    mac.advance()
    # Independently enforce row structure/run identity for streams outside the transport audit.
    observed = {}
    for name in ("scenario_truth", "state_log", "detection_log", "event_log", "busy_audit"):
        need(name in HEADERS and name not in INPUTS, f"Missing log contract for {name}")
        observed[name] = sum(1 for _ in read_table(root, name, rid))
    need(observed["scenario_truth"] and observed["state_log"] and observed["busy_audit"], "Missing observation evidence")
    pairs = {(p["sender_id"], p["receiver_id"]) for p in read_table(root, "pairs", rid)}
    motion_grid = clock.tick(cfg["timing"]["truth_step_s"])
    load_truth(root / "logs", clock, by_tick, {v for pair in pairs for v in pair}, motion_grid, 0, clock.tick(duration))
    next_time, limits = {}, {}
    for pair in pairs:
        birth = max(by_tick[v][0] for v in pair)
        next_time[pair] = ((birth + motion_grid - 1) // motion_grid) * motion_grid
        limits[pair] = min(by_tick[v][1] for v in pair)
    for row in read_table(root, "state_log", rid):
        pair = row["sender_id"], row["receiver_id"]
        t = clock.tick(row["t"])
        need(pair in pairs and next_time[pair] == t <= limits[pair], f"Missing or off-grid state tick for {pair}")
        next_time[pair] += motion_grid
    need(all(next_time[p] >= limits[p] for p in pairs), "Truncated short-run registered state timeline")
    fields = ("actual_entries", "expected_entries", "insertion_failures", "unexplained_teleports", "unexplained_removals")
    need(all(type(traffic.get(k)) is int and traffic[k] >= 0 for k in fields), "Invalid SUMO diagnostics")
    need(traffic["actual_entries"] == traffic["expected_entries"] == len(life)
         and all(traffic[k] == 0 for k in fields[2:]), "SUMO population/traffic reconciliation failed")
    return {"fixed_control_windows": count, "max_busy_error_s": max_busy_error, "logged_rows": observed,
            "normal_removals_before_control": sorted(removed_before_tick), "transport": transport}


def analyze_attempt(attempt):
    """Normal engineering run entry point, after the engine flushes and before metrics.json is committed."""
    root = Path(attempt).resolve()
    cfg, m = doc(root / "config_snapshot.json"), doc(root / "run_manifest.json")
    spec = cfg["resource_probe"]
    need(cfg.get("data_origin") == "simulation" and m.get("scope") == "engineering", "Resource evidence requires a real engineering run")
    mode = spec["logging_mode"]
    need(mode in ("full", "registered_pairs") and m.get("observation_mode") == mode, "Logging mode differs from registration")
    duration = quantity(str(spec["duration_s"]))
    need(duration > 0 and type(spec["background_vehicles"]) is int and spec["background_vehicles"] > 0, "Invalid resource registration")
    life, peak, peak_bg, bound = vehicle_counts(root, m["run_id"], duration)
    engine = engine_evidence(root, m["run_id"], duration)
    need(peak <= engine["peak_active_vehicle_callbacks"] <= bound, "Internal active count does not reconcile with lifetimes")
    need(peak_bg >= spec["background_vehicles"], f"Actual concurrent background peak {peak_bg} is below registration")
    complete = audit_short(root, cfg, m, duration, life)
    return {"scope": "resource_only", "data_origin": "simulation", "data_validity": "valid", "scheme": cfg["scheme"],
            "scenario": cfg["scenario"], "seed": cfg["seed"], "duration_s": float(duration),
            "requested_background_vehicles": spec["background_vehicles"], "peak_concurrent_vehicles": peak,
            "peak_concurrent_background_vehicles": peak_bg, **engine, **complete}


def scientific(metrics):
    """Only instrumentation volume and run identity change with observation mode."""
    result = json.loads(json.dumps(metrics, allow_nan=False))
    result.pop("run_id")
    for k in ("logged_tx", "logged_rx", "observation_mode"):
        result["transport"].pop(k)
    result["state"].pop("busy_audit")
    return result


def comparable_inputs(root, manifest):
    folder = root / "inputs"
    # Reuse the batch runner's XML/path normalization on actual engine inputs. The job-local
    # output mode is the only scientific INI difference allowed for this regression.
    with tempfile.TemporaryDirectory(prefix="tabr_observation_compare_") as directory:
        copy = Path(directory)
        for path in folder.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in (".xml", ".csv", ".cfg", ".ini", ".sumocfg"):
                continue
            dest = copy / path.relative_to(folder)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if path.name == "formal.ini":
                normalized, changes = [], 0
                for line in path.read_text(encoding="utf-8-sig").splitlines():
                    k, sep, val = line.partition("=")
                    if sep and k.strip() == "*.node[*].appl.formalFullLog":
                        need(val.strip() in ("true", "false"), "Invalid effective formalFullLog setting")
                        line, changes = k + "= OBSERVATION_MODE", changes + 1
                    normalized.append(line)
                need(changes == 1, "Missing or ambiguous effective logging mode")
                dest.write_text("\n".join(normalized) + "\n", encoding="utf-8")
            else:
                shutil.copyfile(path, dest)
        need((copy / "formal.ini").is_file() and (copy / "traffic.rou.xml").is_file(), "Missing effective engine inputs")
        return input_fingerprint(copy, manifest.get("generation_dir") or folder)


def compare(full, trimmed):
    results = []
    for name, path in (("full", full), ("registered_pairs", trimmed)):
        root = Path(path).resolve()
        cfg, m = doc(root / "config_snapshot.json"), doc(root / "run_manifest.json")
        finish_check(root, m)
        need(cfg.get("data_origin") == "simulation" and m["observation_mode"] == name and
             "resource_probe" not in cfg, f"{name}: requires a complete, real full-duration scientific attempt")
        duration = quantity(str(cfg["timing"]["duration_s"]))
        life, peak, bgpeak, bound = vehicle_counts(root, m["run_id"], duration)
        resource = engine_evidence(root, m["run_id"], duration)
        need(peak <= resource["peak_active_vehicle_callbacks"] <= bound, "Callback count differs from lifecycle")
        resource.update(peak_concurrent_vehicles=peak, peak_concurrent_background_vehicles=bgpeak)
        metrics = scientific(formal_metrics(root))
        sources = m.get("sources", {})
        need(sources, "Missing simulation source hashes")
        cfg_fields = {k: v for k, v in physics_fields(cfg).items() if k != "observation_mode"}
        results.append((cfg_fields, comparable_inputs(root, m), sources, life, metrics, resource))
    for idx, name in enumerate(("scientific config", "physical XML/CSV inputs", "source hashes", "per-vehicle lifecycle/TX/RX", "scientific metrics and trajectories")):
        need(results[0][idx] == results[1][idx], f"Full/trimmed mismatch in {name}")
    metrics = results[0][4]
    return {"status": "equivalent", "origin": "simulation", "seed": metrics["seed"], "scheme": metrics["scheme"],
            "scenario": metrics["scenario"], "vehicles": len(results[0][3]),
            "trajectory_sha256_by_vehicle": metrics["state"]["trajectory_sha256_by_vehicle"],
            "scientific_metrics_sha256": hashlib.sha256(json.dumps(metrics, sort_keys=True, allow_nan=False).encode()).hexdigest(),
            "resources": {"full": results[0][5], "registered_pairs": results[1][5]}}


def ladder(attempts, scales):
    need(len(set(scales)) == len(scales) and list(scales) == sorted(scales) and all(x > 0 for x in scales), "Expected increasing unique scales")
    stages, completed = [], []
    last_ended = None
    for attempt in attempts:
        root = Path(attempt).resolve()
        m = doc(root / "run_manifest.json")
        finish_check(root, m)
        start, end = (datetime.fromisoformat(m[k]) for k in ("started_at", "completed_at"))
        need(start <= end and (last_ended is None or start >= last_ended), "Resource runs overlap or are out of order")
        last_ended = end
        item = analyze_attempt(root)
        n = item["requested_background_vehicles"]
        need(n in scales, f"Unexpected registered size {n}")
        if not completed or completed[-1] != n:
            need(len(completed) < len(scales) and n == scales[len(completed)], f"Missing earlier size before {n}")
            completed.append(n)
        stages.append(item)
    remaining = list(scales[len(completed):])
    includes_g1 = any(r["scheme"] == "G1" for r in stages)
    return {"status": "complete" if not remaining and includes_g1 else "incomplete",
            "completed_scales": completed, "remaining_scales": remaining, "includes_g1": includes_g1, "stages": stages}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="mode", required=True)
    sub.add_parser("plan")
    audit = sub.add_parser("audit")
    audit.add_argument("attempt", type=Path)
    cmp = sub.add_parser("compare")
    cmp.add_argument("full", type=Path)
    cmp.add_argument("trimmed", type=Path)
    stairs = sub.add_parser("ladder")
    stairs.add_argument("attempts", nargs="*", type=Path)
    stairs.add_argument("--scales", type=int, nargs="+", default=SCALES)
    for parser in (audit, cmp, stairs):
        parser.add_argument("--output", type=Path)
    args = p.parse_args()
    try:
        if args.mode == "plan":
            out = {"mode": "resource_only", "serial": True, "initial_scales": list(SCALES),
                   "example_seed": 200, "example_scheme": "G1", "candidate_scales_must_reach_actual_concurrency": True}
        elif args.mode == "compare":
            out = compare(args.full, args.trimmed)
        elif args.mode == "ladder":
            out = ladder(args.attempts, args.scales)
        else:
            finish_check(args.attempt, doc(args.attempt / "run_manifest.json"))
            out = analyze_attempt(args.attempt)
        code = 40 if out.get("status") == "incomplete" else 0
    except (ValueError, ArithmeticError, OSError, KeyError, TypeError, csv.Error) as error:
        out, code = {"status": "invalid", "reason": str(error)}, 30
    payload = json.dumps(out, allow_nan=False, indent=2, ensure_ascii=False)
    if getattr(args, "output", None):
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return code


if __name__ == "__main__":
    sys.exit(main())
