"""Rebuild P2 engineering evidence without launching a simulation. Run from any cwd."""

import argparse
import csv
import hashlib
from itertools import groupby, zip_longest
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import experiment
from resource_probe import SCALES


PROJECT = Path(__file__).resolve().parents[1]
LIMIT_M = 1e-5  # Same absolute coordinate bound as the live front-bumper check.


def need(condition, reason):
    if not condition:
        raise ValueError(reason)


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def csv_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        yield from csv.DictReader(f)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def track_grid(path):
    # Sort only vehicles within the current timestamp; files can have different per-tick order.
    for t, chunk in groupby(csv_rows(path), key=lambda r: float(r["t"])):
        cars = {}
        for r in chunk:
            v = r["vehicle_id"]
            need(v not in cars, f"Duplicate coordinate for {v} at {t}")
            cars[v] = float(r["position_m"]), float(r["speed_mps"])
        yield t, cars


def coordinate_check(attempt, independent):
    tracked = {
        r[k] for r in csv_rows(attempt / "inputs/pairs.csv") for k in ("sender_id", "receiver_id")
    }
    audit = {}
    for r in csv_rows(attempt / "logs/coordinate_audit.csv"):
        v = r["vehicle_id"]
        err = abs(float(r["veins_front_x_m"]) - float(r["sumo_lane_position_m"]))
        need(v not in audit and err <= LIMIT_M, "First-sample physical coordinate mismatch")
        audit[v] = err
    need(set(audit) == tracked, "Preregistered position cross-check incomplete")
    samples = ticks = 0
    dx = dv = 0.0
    target_extent = {}
    for lhs, rhs in zip_longest(
        track_grid(attempt / "logs/scenario_truth.csv"), track_grid(independent / "scenario_truth.csv")
    ):
        need(
            lhs and rhs and lhs[0] == rhs[0] and lhs[1].keys() == rhs[1].keys(),
            "Co-simulation and SUMO-only (time,vehicle) grids differ",
        )
        for v, (x, speed) in lhs[1].items():
            ref_x, ref_v = rhs[1][v]
            dx = max(dx, abs(x - ref_x))
            dv = max(dv, abs(speed - ref_v))
            if v in ("lead", "follow"):
                a, b, c = target_extent.get(v, (x, x, 0.0))
                target_extent[v] = min(a, x), max(b, x), max(c, abs(speed))
        samples += len(lhs[1])
        ticks += 1
    need(samples and dx <= LIMIT_M and dv <= 1e-9, "Independent full-trajectory comparison failed")
    return dict(
        registered_vehicles=len(tracked),
        first_coordinate_abs_error_m_by_vehicle=audit,
        matched_samples=samples,
        matched_ticks=ticks,
        maximum_position_difference_m=dx,
        maximum_speed_difference_mps=dv,
        speed_tolerance_mps=1e-9,
        position_tolerance_m=LIMIT_M,
        target_extent_min_x_max_x_max_abs_speed=target_extent,
    )


def corruption_check(batch, attempt, temp_parent):
    with tempfile.TemporaryDirectory(prefix="p2_verify_", dir=temp_parent) as tmp:
        clone = Path(tmp) / "batch"
        shutil.copytree(batch, clone)

        def verify():
            cmd = [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                str(PROJECT / "tools/experiment.py"),
                "verify",
                "--batch",
                str(clone),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=120)
            if proc.returncode not in (0, 30):
                raise ValueError(
                    f"verify unexpectedly returned {proc.returncode}: {proc.stdout[-350:]} {proc.stderr[-350:]}"
                )
            return proc.returncode, json.loads(proc.stdout)

        baseline, _ = verify()
        need(baseline == 0, "Clean restored probe failed verification")
        src = attempt / "logs/control_log.csv"
        copied = clone / src.relative_to(batch)
        with copied.open("r+b") as f:
            f.seek(-12, 2)
            f.truncate()
        code, report = verify()
        need(code == 30, "Corrupted physical log accepted")
        return dict(
            clean_copy_exit_code=baseline,
            damaged_copy_exit_code=code,
            corruption="truncate final 12 bytes of logs/control_log.csv",
            normal_verify=report,
        )


def build(project, temp_parent, ladder_report=None):
    checked = {}
    resources = {}
    scientific = []
    loss = None
    compare = None
    result = project / "results/probes"
    receipts = []
    for batch in sorted(result.glob("*/*/batch.json")):
        b = read(batch)
        if b.get("scope") != "engineering" or not batch.parent.parent.name.startswith("p2_"):
            continue
        for job in b["jobs"]:
            for token in b["attempts"][job["job_id"]]:
                p = experiment.attempt_dir(batch.parent, b, job, token)
                manifest = read(p / "run_manifest.json")
                if manifest["status"] != "completed":
                    continue
                state, m = experiment.inspect_attempt(p, b, job)
                need(state == "completed", f"Completed receipt has changed files: {p}")
                c = read(p / "config_snapshot.json")
                metrics = read(p / "metrics.json")
                need(
                    metrics["data_origin"] == "simulation" and metrics["data_validity"] == "valid",
                    "Expected valid real engineering data",
                )
                ref = p.relative_to(project).as_posix()
                receipts.append(ref)
                if c.get("resource_probe"):
                    n = c["resource_probe"]["background_vehicles"]
                    scheme = job["scheme"]
                    need(
                        metrics["peak_concurrent_background_vehicles"] >= n
                        and metrics["exact_peak_project_log_streams"] <= 32,
                        f"Resource threshold failed at {n}/{scheme}",
                    )
                    resources[n, scheme] = dict(
                        attempt=ref,
                        peak_background=metrics["peak_concurrent_background_vehicles"],
                        peak_project_log_streams=metrics["exact_peak_project_log_streams"],
                        peak_handles=metrics["sampled_peak_handles"],
                        peak_private_bytes=metrics["sampled_peak_private_bytes"],
                        actual_log_bytes=metrics["log_output_bytes"],
                    )
                    continue
                name = batch.parent.parent.name
                sc = job["scenario"]
                if c.get("engineering_geometry", {}).get("background_vehicles") != 16 or sc not in (
                    "S0",
                    "S1",
                    "S2",
                ):
                    continue
                independent = project / "verification/p2/sumo" / ("engineering16_" + sc) / "sumo_only"
                need(read(independent / "validation.json")["valid"], "Independent SUMO run not valid")
                trajectory = coordinate_check(p, independent)
                entry = dict(
                    attempt=ref,
                    scenario=sc,
                    scheme=job["scheme"],
                    seed=job["seed"],
                    coordinate=trajectory,
                    physical_events=metrics["physical_events"],
                    run_metrics=metrics["run"],
                    trajectory_sha256=digest(metrics["state"]["trajectory_sha256_by_vehicle"]),
                    independent_reference=independent.relative_to(project).as_posix(),
                )
                if name == "p2_S1_radio_loss":
                    e = metrics["events"]
                    need(
                        len(e) == 1
                        and e[0]["physical_occurred"]
                        and e[0]["no_receive"]
                        and e[0]["deadline_miss"]
                        and e[0]["D_info_s"] is None
                        and e[0]["restricted_wait_s"] == e[0]["observation_s"] == 2
                        and e[0]["right_censored"]
                        and metrics["transport"]["all_rx"] == 0,
                        "Real miss classification failed",
                    )
                    checked["S1_radio_loss"] = {
                        **entry,
                        "event": e[0],
                        "data_validity": "valid",
                        "noise_floor_dbm": c["wireless"]["noise_floor_dbm"],
                        "all_rx": metrics["transport"]["all_rx"],
                    }
                    loss = (batch.parent, p)
                elif sc == "S0":
                    need(
                        not metrics["events"]
                        and metrics["transport"]["given_priors"] == 1
                        and metrics["run"]["deadline_miss_fraction"] is None,
                        "S0 prior/event contract failed",
                    )
                    for v in metrics["state"]["pairs"].values():
                        if v["role"] == "target":
                            need(
                                v["unknown_fraction"] == 0 and v["mean_abs_error_m"] < LIMIT_M,
                                "S0 state lost",
                            )
                    ext = trajectory["target_extent_min_x_max_x_max_abs_speed"]
                    need(
                        ext["lead"] == (3000.0, 3000.0, 0.0) and ext["follow"] == (2970.0, 2970.0, 0.0),
                        "Targets moved in S0",
                    )
                    checked["S0"] = {**entry, "given_priors": 1, "event_count": 0}
                elif sc == "S2":
                    ev = metrics["physical_events"]
                    need(
                        len(metrics["events"]) == len(ev) == len({x["sender_id"] for x in ev}) == 4
                        and all(x["physical_occurred"] for x in ev),
                        "S2 four-sender denominator failed",
                    )
                    checked["S2"] = entry
                else:
                    need(
                        metrics["run"]["physical_event_n"] == metrics["run"]["event_pair_n"] == 1,
                        "S1 physical event missing",
                    )
                    checked["S1"] = entry
                if name != "p2_S1_radio_loss":
                    common = {
                        k: c[k]
                        for k in (
                            "wireless",
                            "motion",
                            "geometry",
                            "control",
                            "timing",
                            "engineering_geometry",
                            "seed",
                            "load_id",
                            "rng",
                            "scenario",
                        )
                    }
                    common["executable_sha256"] = m["sources"]["EIE575_TABR_Starter.exe"]
                    common["ned_sha256"] = {k: v for k, v in m["sources"].items() if k.endswith(".ned")}
                    common["effective_files"] = {
                        k: m["files"]["inputs/" + k]
                        for k in (
                            "events.csv",
                            "pairs.csv",
                            "prior.csv",
                            "phases.csv",
                            "vehicles.csv",
                            "radio.xml",
                            "highway.net.xml",
                            "traffic.rou.xml",
                            "traffic.sumo.cfg",
                        )
                    }
                    entry["comparison_conditions"] = digest(common)
                    scientific.append(entry)
                if name == "p2_S1_full":
                    compare = metrics
    results_by_condition = {}
    for e in scientific:
        g = results_by_condition.setdefault(e["comparison_conditions"], {})
        hashes = g.setdefault(e["scheme"], set())
        hashes.add(e["trajectory_sha256"])
    groups = []
    for cond, schemes in results_by_condition.items():
        all_hashes = set().union(*schemes.values())
        need(len(all_hashes) == 1, "Same traffic and seed produced different motion")
        groups.append(
            dict(
                conditions_sha256=cond,
                scenario=next(x["scenario"] for x in scientific if x["comparison_conditions"] == cond),
                schemes=sorted(schemes),
                trajectory_sha256=next(iter(all_hashes)),
                all_four_present=set(schemes) == {"G0", "G1", "G2", "G3"},
                attempts=[x["attempt"] for x in scientific if x["comparison_conditions"] == cond],
            )
        )
    directory = project / "verification/p2/engine"
    pair_file = directory / "compare_s1.json"
    equivalent = None
    if pair_file.is_file() and compare:
        eq = read(pair_file)
        need(
            eq["status"] == "equivalent"
            and eq["origin"] == "simulation"
            and eq["trajectory_sha256_by_vehicle"] == compare["state"]["trajectory_sha256_by_vehicle"],
            "Full/trimmed receipt mismatch",
        )
        equivalent = dict(
            source=pair_file.relative_to(project).as_posix(),
            status=eq["status"],
            seed=eq["seed"],
            scientific_metrics_sha256=eq["scientific_metrics_sha256"],
        )
    damaged = corruption_check(*loss, temp_parent) if loss else None
    missing = {str(n): [s for s in ("G0", "G1") if (n, s) not in resources] for n in SCALES}
    missing = {k: v for k, v in missing.items() if v}
    stages = dict(
        status="pending" if missing else "passed",
        missing_scale_scheme=missing,
        completed={f"{n}_{s}": v for (n, s), v in sorted(resources.items())},
    )
    if ladder_report is None:
        ladder_report = project / "verification/p2/resource_ladder.json"
    if ladder_report.is_file():
        raw = read(ladder_report)
        need(
            raw.get("status") == "complete" or missing,
            "Resource ladder receipt inconsistent with measurements",
        )
        stages["independent_ladder_report"] = dict(
            path=str(ladder_report),
            status=raw.get("status"),
            completed_scales=raw.get("completed_scales"),
        )
    wait = []
    for key in ("S0", "S1", "S2", "S1_radio_loss"):
        if key not in checked:
            wait.append(key)
    if not equivalent:
        wait.append("full_trimmed")
    if not damaged:
        wait.append("real_corruption_verify")
    for scenario in ("S0", "S1", "S2"):
        if not any(x["all_four_present"] and x["scenario"] == scenario for x in groups):
            wait.append("four_scheme_trajectory_" + scenario)
    return dict(
        scope="P2_engineering_evidence",
        status="pending" if (missing or wait) else "passed",
        functional_status="pending" if wait else "passed",
        pending_functional=wait,
        functional=checked,
        full_trimmed=equivalent,
        corrupted_copy=damaged,
        four_scheme=groups,
        resource_ladder=stages,
        verified_attempts=receipts,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=PROJECT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--temp-parent", type=Path)
    parser.add_argument("--ladder-report", type=Path)
    args = parser.parse_args()
    project = args.project.resolve()
    report = build(project, args.temp_parent, args.ladder_report)
    out = args.output or project / "verification/p2/engine/functional_acceptance.json"
    experiment.save(out, report)
    print(
        json.dumps(
            {
                "output": str(out),
                "status": report["status"],
                "pending_functional": report["pending_functional"],
                "pending_resources": report["resource_ladder"]["missing_scale_scheme"],
            }
        )
    )
