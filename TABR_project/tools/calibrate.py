"""Evaluate E0 evidence and choose the next pre-registered calibration runs."""
import argparse
from bisect import bisect_right
import csv
import hashlib
import json
import math
import sys
from pathlib import Path
from statistics import fmean


def read_json(path):
    with path.open(encoding="utf-8-sig") as stream:
        return json.load(stream)


def records(path, fields):
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not fields.issubset(reader.fieldnames or ()):
            raise ValueError(f"{path.name}: missing fields {sorted(fields - set(reader.fieldnames or []))}")
        yield from reader


def number(x):
    n = float(x)
    if not math.isfinite(n):
        raise ValueError(f"Non-finite measurement {x!r}")
    return n


def grid(x, step, resolution):
    t = number(x)
    i = round(t / step)
    if abs(i * step - t) > resolution + 4 * math.ulp(t):
        raise ValueError(f"Time {t} does not lie on the control grid")
    return i


def quantile(sample, q):
    values = sorted(sample)
    index = (len(values) - 1) * q
    low = math.floor(index)
    return values[low] + (index - low) * (values[math.ceil(index)] - values[low])


def design_key(cfg):
    # Keep the complete shared scientific configuration; run identities and varied axes are recorded separately.
    common = {k: cfg[k] for k in ("schema_version", "experiment_id", "deployment_mode", "wireless", "motion",
                                   "control", "analysis", "geometry", "rng", "seeds", "calibration", "scenarios")}
    common["timing"] = {k: v for k, v in cfg["timing"].items() if k not in {"duration_s", "warmup_s"}}
    return hashlib.sha256(json.dumps(common, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verify_traffic(attempt, targets, duration, step, resolution):
    diag = read_json(attempt / "logs/sumo_diagnostics.json")
    for key in ("expected_entries", "actual_entries", "insertion_failures",
                "unexplained_teleports", "unexplained_removals"):
        if type(diag.get(key)) is not int or diag[key] < 0:
            raise ValueError(f"Invalid SUMO diagnostic {key}")
    if diag["expected_entries"] < len(targets) or diag["expected_entries"] != diag["actual_entries"]:
        raise ValueError("Expected and actual vehicle entries do not reconcile")
    if any(diag[k] for k in ("insertion_failures", "unexplained_teleports", "unexplained_removals")):
        raise ValueError("SUMO reports insertion failure or unexplained disappearance")
    life = {}
    for row in records(attempt / "logs/lifecycle.csv", {"vehicle_id", "enter_s", "exit_s", "busy_audited"}):
        car = row["vehicle_id"]
        if car not in targets:
            continue
        if car in life or row["busy_audited"] != "1":
            raise ValueError(f"{car}: duplicate lifecycle or missing independent MAC audit")
        entry = grid(row["enter_s"], step, resolution)
        end = grid(row["exit_s"], step, resolution)
        if not (0 <= entry <= 1 and end == grid(duration, step, resolution)):
            raise ValueError(f"{car}: incomplete target exposure")
        life[car] = (entry, end)
    if set(life) != set(targets):
        raise ValueError("Missing calibration target lifecycle")
    return life, diag


def independent_busy(path, life, step, resolution):
    """Read interleaved MAC transitions once; keep only two target timelines binned by window."""
    totals = {car: [0.0] * end for car, (_, end) in life.items()}
    last = {}

    def add(car, start, stop):
        start = max(start, life[car][0] * step)
        stop = min(stop, life[car][1] * step)
        for tick in range(max(0, math.floor(start / step)), min(len(totals[car]), math.ceil(stop / step))):
            totals[car][tick] += max(0.0, min(stop, (tick + 1) * step) - max(start, tick * step))

    for row in records(path, {"vehicle_id", "transition_time", "busy_state"}):
        car = row["vehicle_id"]
        if car not in life:
            continue
        t = number(row["transition_time"])
        if row["busy_state"] not in {"0", "1"}:
            raise ValueError(f"{car}: invalid busy flag")
        busy = row["busy_state"] == "1"
        if car in last:
            previous, was_busy = last[car]
            if t < previous - resolution or t > life[car][1] * step + resolution:
                raise ValueError(f"{car}: MAC transition outside its ordered lifecycle")
            if was_busy:
                add(car, previous, t)
        elif busy or abs(t - life[car][0] * step) > resolution:
            raise ValueError(f"{car}: missing initial idle MAC marker")
        last[car] = (t, busy)
    if set(last) != set(life):
        raise ValueError("Missing target MAC transition data")
    for car, (t, busy) in last.items():
        if busy:
            add(car, t, life[car][1] * step)
    return totals


class CbrPopulations:
    """Aggregate CBR by physical exposure, retaining only one accumulator per car/window."""

    def __init__(self, cfg, targets, monitored, bounds):
        self.targets = set(targets)
        self.monitored = set(monitored)
        self.bounds = bounds
        self.length_m = cfg["geometry"]["length_m"]
        self.edge_distance_m = cfg["geometry"].get("edge_distance_m", 1000)
        self.data = {name: [{} for _ in bounds[1:]] for name in
                     ("target", "registered_background_receivers", "background_at_road_edges", "all_vehicles")}

    def add(self, car, start, end, cbr, position):
        if start >= end or not 0 <= cbr <= 1 or not 0 <= position <= self.length_m:
            raise ValueError(f"{car}: invalid control exposure, CBR, or road-relative lane_position_m")
        i = max(0, bisect_right(self.bounds, start) - 1)
        if i >= len(self.bounds) - 1 or end <= self.bounds[0]:
            return
        groups = ["all_vehicles"]
        if car in self.targets:
            groups.append("target")
        else:
            if car in self.monitored:
                groups.append("registered_background_receivers")
            if min(position, self.length_m - position) <= self.edge_distance_m:
                groups.append("background_at_road_edges")
        while i < len(self.bounds) - 1 and self.bounds[i] < end:
            exposure = min(end, self.bounds[i + 1]) - max(start, self.bounds[i])
            if exposure > 0:
                for group in groups:
                    sums = self.data[group][i].setdefault(car, [0.0, 0.0])
                    sums[0] += cbr * exposure
                    sums[1] += exposure
            i += 1

    def summary(self):
        output = {}
        for group, windows in self.data.items():
            rows = []
            for i, vehicles in enumerate(windows):
                if group == "registered_background_receivers":
                    required_s = self.bounds[i + 1] - self.bounds[i]
                    if set(vehicles) != self.monitored or any(abs(v[1] - required_s) > 1e-8 for v in vehicles.values()):
                        raise ValueError(f"Registered background receiver CBR coverage incomplete in window {i + 1}")
                seconds = math.fsum(v[1] for v in vehicles.values())
                rows.append({"start_s": self.bounds[i], "end_s": self.bounds[i + 1],
                             "exposed_vehicles": len(vehicles), "observed_vehicle_seconds": seconds,
                             "vehicle_equal_mean_cbr": fmean(a / b for a, b in vehicles.values()) if vehicles else None,
                             "exposure_weighted_mean_cbr": math.fsum(v[0] for v in vehicles.values()) / seconds
                             if seconds else None})
            output[group] = rows
        return {"road_edge_distance_m": self.edge_distance_m, "populations": output}


def measurements(attempt, life, cfg, step, resolution, warmup, populations):
    mac = independent_busy(attempt / "logs/busy_audit.csv", life, step, resolution)
    control = {car: {} for car in life}
    supply = {car: {} for car in life}
    errors = {car: 0.0 for car in life}
    fields = {"vehicle_id", "window_start", "window_end", "tick_time", "C", "Cbar", "b", "r",
              "busy_duration_s", "lane_position_m"}
    for row in records(attempt / "logs/control_log.csv", fields):
        car = row["vehicle_id"]
        populations.add(car, number(row["window_start"]), number(row["window_end"]), number(row["C"]),
                        number(row["lane_position_m"]))
        if car not in life:
            continue
        start, end, tick = (grid(row[k], step, resolution) for k in ("window_start", "window_end", "tick_time"))
        if start in control[car] or tick != end or end != start + 1 or not (life[car][0] <= start < life[car][1]):
            raise ValueError(f"{car}: duplicate/off-grid control window {start}")
        raw, smooth, base, rate, busy = (number(row[k]) for k in ("C", "Cbar", "b", "r", "busy_duration_s"))
        bounds = cfg["control"]
        if not (0 <= raw <= 1 and 0 <= smooth <= 1 and bounds["b_min_hz"] - 1e-9 <= base <= bounds["b_max_hz"] + 1e-9
                and bounds["r_min_hz"] - 1e-9 <= rate <= bounds["r_max_hz"] + 1e-9):
            raise ValueError(f"{car}: controller output outside configured bounds")
        error = abs(mac[car][start] - busy)
        errors[car] = max(errors[car], error)
        if error > cfg["analysis"]["busy_integral_tolerance_s"] or abs(busy / step - raw) > 1e-8:
            raise ValueError(f"{car}: independent busy/CBR audit failed at window {start}")
        control[car][start] = (raw, smooth, rate)
    first = grid(warmup - 10, step, resolution)
    final = grid(warmup + 90, step, resolution)
    for row in records(attempt / "logs/local_supply.csv", {"vehicle_id", "t", "background_count"}):
        car = row["vehicle_id"]
        if car not in life:
            continue
        index = grid(row["t"], step, resolution)
        if first <= index < final:
            value = int(row["background_count"])
            if index in supply[car] or value < 0:
                raise ValueError(f"{car}: duplicate/invalid local supply")
            supply[car][index] = value
    for car, (entry, end) in life.items():
        if len(control[car]) != end - entry or len(supply[car]) != final - first:
            raise ValueError(f"{car}: missing control or local supply ticks")
    return control, supply, errors


def evaluate(attempt):
    attempt = Path(attempt)
    cfg = read_json(attempt / "config_snapshot.json")
    cal = cfg["calibration"]
    density, seed = cfg["density_per_lane_km"], cfg["seed"]
    warmup = cfg["timing"]["warmup_s"]
    duration = cfg["timing"]["duration_s"]
    step = cfg["timing"]["control_step_s"]
    resolution = cfg["timing"]["time_resolution_s"]
    origin = cfg.get("data_origin", "simulation")
    if cfg.get("stage") == "synthetic" or cfg.get("scope") == "synthetic":
        origin = "synthetic"
    if cfg["scenario"] != "E0" or cfg["scheme"] != "G0" or origin not in {"simulation", "synthetic"}:
        raise ValueError("Calibration requires E0, G0 and an identifiable data origin")
    if (seed not in cfg["seeds"]["calibration"] or density not in cal["initial_densities"] + cal["additional_densities"]
            or warmup not in cal["warmups_s"] or duration != warmup + 100 or step != 0.1):
        raise ValueError("Run falls outside the pre-registered calibration grid")
    registered = list(records(attempt / "inputs/pairs.csv", {"role", "sender_id", "receiver_id"}))
    pairs = [p for p in registered if p["role"] == "target"]
    if len(pairs) != 1 or pairs[0]["sender_id"] == pairs[0]["receiver_id"]:
        raise ValueError("E0 requires one distinct target lead/follow pair")
    if next(records(attempt / "inputs/events.csv", {"planned_event_id"}), None) is not None:
        raise ValueError("E0 must have no planned brake")
    targets = [pairs[0]["sender_id"], pairs[0]["receiver_id"]]
    monitored = [p["receiver_id"] for p in registered if p["role"] == "background"]
    if len(monitored) != cfg["geometry"]["background_pair_count"] or len(set(monitored)) != len(monitored):
        raise ValueError("Missing or duplicate pre-registered background receiver")
    if set(monitored) & set(targets):
        raise ValueError("Target also appears as background receiver")
    life, diagnostic = verify_traffic(attempt, targets, duration, step, resolution)
    bounds = [warmup - 10, warmup - 5, warmup, warmup + 30, warmup + 60, warmup + 90]
    populations = CbrPopulations(cfg, targets, monitored, bounds)
    control, supply, errors = measurements(attempt, life, cfg, step, resolution, warmup, populations)
    windows = [range(grid(a, step, resolution), grid(b, step, resolution)) for a, b in zip(bounds, bounds[1:])]
    metrics, unstable, low_errors, high_errors = {}, [], [], []
    # Thresholds are inclusive; the tiny epsilon covers roundoff from summation only.
    epsilon = 1e-12
    for car in targets:
        row = []
        for i, interval in enumerate(windows):
            smoothed = [control[car][tick][1] for tick in interval]
            item = {"start_s": bounds[i], "end_s": bounds[i + 1],
                    "raw_cbr_mean": fmean(control[car][tick][0] for tick in interval),
                    "smoothed_p90_minus_p10": quantile(smoothed, .9) - quantile(smoothed, .1),
                    "local_count_mean": fmean(supply[car][tick] for tick in interval)}
            row.append(item)
            label = f"{car}:win{i + 1}"
            if item["smoothed_p90_minus_p10"] > cal["max_smoothed_span"] + epsilon:
                unstable.append(label + ":oscillation")
            if item["raw_cbr_mean"] > cal["low_max"] + epsilon:
                low_errors.append(label + ":raw_cbr")
            if not cal["high_min"] - epsilon <= item["raw_cbr_mean"] <= cal["high_max"] + epsilon:
                high_errors.append(label + ":raw_cbr")
        pre_local = (row[0]["local_count_mean"] + row[1]["local_count_mean"]) / 2
        if pre_local == 0:
            raise ValueError(f"{car}: zero pre-window local population, relative supply change undefined")
        if abs(row[1]["raw_cbr_mean"] - row[0]["raw_cbr_mean"]) > cal["max_prewindow_drift"] + epsilon:
            unstable.append(car + ":pre_drift")
        for idx, item in enumerate(row[2:], 3):
            if abs(item["raw_cbr_mean"] - row[1]["raw_cbr_mean"]) > cal["max_longwindow_drift"] + epsilon:
                unstable.append(f"{car}:win{idx}:post_drift")
            drift = abs(item["local_count_mean"] / pre_local - 1)
            item["local_relative_change_abs"] = drift
            if drift > cal["max_supply_drift"] + epsilon:
                unstable.append(f"{car}:win{idx}:supply_drift")
        post = range(windows[2].start, windows[-1].stop)
        feedback = sum(1 + 1e-6 < control[car][tick][2] <= 9.5 for tick in post) / len(post)
        if feedback + epsilon < cal["feedback_exposure_min"]:
            high_errors.append(car + ":feedback_exposure")
        metrics[car] = {"windows": row, "feedback_exposure_fraction": feedback, "max_busy_error_s": errors[car]}
    return {"density": density, "seed": seed, "warmup_s": warmup, "origin": origin, "design_sha256": design_key(cfg),
            "valid": True, "stable": not unstable, "low": not unstable and not low_errors,
            "high": not unstable and not high_errors, "stability_failures": unstable, "low_failures": low_errors,
            "high_failures": high_errors, "targets": metrics, "traffic": diagnostic, "spatial_cbr": populations.summary(),
            "target_equal_weight_window_cbr": [fmean(metrics[c]["windows"][i]["raw_cbr_mean"] for c in targets)
                                                for i in range(len(windows))]}


def geometry_exclusion_index(cfg, paths, origin):
    """Reproduce preregistration from the effective design; a geometry rejection contains no wireless result."""
    from formal_scenario import ScenarioGeometryError, registrations

    geom = cfg["geometry"]
    out = {}
    for item in paths:
        path = Path(item).resolve()
        actual = read_json(path)
        density, seed = actual["density_per_km_lane"], actual["seed"]
        if (type(seed) is not int or seed not in cfg["seeds"]["calibration"]
                or density not in cfg["calibration"]["additional_densities"] or density <= 0):
            raise ValueError("Geometry exclusion has an unregistered sparse density/seed")
        expected_origin = "scenario_preregistration" if origin == "simulation" else "synthetic"
        if (actual.get("data_origin") != expected_origin or actual.get("source") != "formal_scenario.registrations"
                or actual.get("simulation_started") is not False or actual.get("valid") is not False
                or actual.get("distribution") != "uniform_full_road" or actual.get("stage") != "core"
                or actual.get("scenario") != "E0" or actual.get("eligible_background_pairs") != 0
                or actual["required_background_pairs"] != geom["background_pair_count"]
                or 1000 / density <= geom["pair_max_distance_m"]):
            raise ValueError("Geometry report does not establish the authorized sparse full-road exclusion")
        lanes = geom["background_lanes"]
        per_lane = density * geom["length_m"] / 1000
        f = actual["filtering"]
        if (not per_lane.is_integer() or actual["background_prefill_n"] != per_lane * lanes
                or actual["uniform_spacing_m"] != 1000 / density
                or actual["complete_adjacent_pairs_scanned"] != (per_lane - 1) * lanes
                or f["disjoint_counts"] is not True or f["eligible_pairs"] != 0
                or f["excluded_distance"] + f["excluded_outside_window"] != actual["complete_adjacent_pairs_scanned"]):
            raise ValueError("Geometry candidate enumeration/road population is inconsistent")
        source_cfg = {**cfg, "synthetic": origin == "synthetic",
                      "timing": {**cfg["timing"], "warmup_s": cfg["calibration"]["warmups_s"][0],
                                 "duration_s": cfg["calibration"]["warmups_s"][0] + 100}}
        try:
            registrations(source_cfg, {"scenario": "E0", "scheme": "G0", "density_per_km_lane": density, "seed": seed})
        except ScenarioGeometryError as error:
            if error.report != actual:
                raise ValueError("Geometry rejection does not match current generator/configuration/seed")
        else:
            raise ValueError("Current preregistration produces a valid geometry; exclusion rejected")
        grouped = out.setdefault(density, {})
        if seed in grouped:
            raise ValueError("Duplicate preregistration evidence for the same density/seed")
        grouped[seed] = {"seed": seed, "report_file": str(path), "report_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                         "report": actual}
    return out


def decide(cfg, reports, origin="simulation", geometry_exclusions=()):
    """Reports represent distinct successful engineering attempts. Invalid runs require repair."""
    cal = cfg["calibration"]
    initial, extra, warmups = (cal[k] for k in ("initial_densities", "additional_densities", "warmups_s"))
    seeds = cfg["seeds"]["calibration"]
    index = {}
    version = design_key(cfg)
    geometry = geometry_exclusion_index(cfg, geometry_exclusions, origin)
    excluded, requested_geometry = set(), set()
    for rec in reports:
        key = (rec["density"], rec["warmup_s"], rec["seed"])
        if key in index:
            raise ValueError(f"Duplicate calibration key {key}; submit one engineering-valid attempt per key")
        if key[0] in geometry:
            raise ValueError(f"Conflicting geometry exclusion and connected-simulator result at density {key[0]}")
        if rec["origin"] != origin or rec["design_sha256"] != version:
            raise ValueError("Mixed data origin or calibration design")
        if key[0] not in initial + extra or key[1] not in warmups or key[2] not in seeds or rec["valid"] is not True:
            raise ValueError(f"Invalid calibration attempt {key}")
        index[key] = rec

    used = set()
    accepted = {"low": {}, "high": {}}
    tested = []

    def result(status, **fields):
        if index.keys() - used:
            raise ValueError(f"Unscheduled/prior-decision runs present: {sorted(index.keys() - used)}")
        return {"status": status, "origin": origin, "completed_unique_runs": len(used),
                "geometry_exclusions": [{"density_per_lane_km": d, "reports": list(by_seed.values()),
                                          "both_seeds_verified": set(by_seed) == set(seeds),
                                          "used_in_search": d in excluded, "requested_by_search": d in requested_geometry,
                                          "wireless_runs": 0, "measured_cbr": None}
                                         for d, by_seed in sorted(geometry.items())],
                "design_sha256": version, "candidate_decisions": tested, **fields}

    def pair(density, warm, phase):
        keys = [(density, warm, seed) for seed in seeds]
        used.update(k for k in keys if k in index)
        missing = [{"density_per_lane_km": d, "warmup_s": w, "seed": s, "scenario": "E0", "scheme": "G0"}
                   for d, w, s in keys if (d, w, s) not in index]
        if missing:
            return None, missing
        runs = [index[k] for k in keys]
        flags = {k: all(r[k] for r in runs) for k in ("stable", "low", "high")}
        tested.append({"density": density, "warmup_s": warm, "phase": phase, **flags,
                       "per_seed": [{k: r[k] for k in ("seed", "stable", "low", "high", "stability_failures",
                                                         "low_failures", "high_failures")} for r in runs]})
        if phase != "common_warmup":
            for label in accepted:
                if flags[label]:
                    accepted[label][density] = warm
        return flags, []

    def finalize():
        selected = {label: min(entries) for label, entries in accepted.items()}
        original = {label: accepted[label][density] for label, density in selected.items()}
        common = max(original.values())
        if original["low"] != original["high"]:
            pending = []
            ok = True
            for label, density in selected.items():
                flags, missing = pair(density, common, "common_warmup")
                pending.extend(missing)
                if flags:
                    ok &= flags[label]
            if pending:
                return result("common_warmup_required", next_runs=pending, selected=selected, warmup_s=common)
            if not ok:
                return result("common_warmup_failed", selected=selected, warmup_s=common)
        return result("selected", selected=selected, warmup_s=common, eligible_for_e1=origin == "simulation")

    first = warmups[0]
    pending, unstable = [], []
    for density in initial:
        flags, missing = pair(density, first, "initial")
        pending.extend(missing)
        if flags and not flags["stable"]:
            unstable.append(density)
    if pending:
        return result("initial_runs_required", next_runs=pending)
    if all(accepted.values()):
        return finalize()
    for density in unstable + extra:
        if density in geometry:
            requested_geometry.add(density)
            missing_seeds = [s for s in seeds if s not in geometry[density]]
            if missing_seeds:
                return result("geometry_evidence_required", density_per_lane_km=density,
                              missing_preregistration_seeds=missing_seeds, next_runs=[])
            excluded.add(density)
            tested.append({"density": density, "phase": "geometry_excluded", "seeds": list(seeds),
                           "reason": "uniform_spacing_exceeds_registered_maximum", "measured_cbr": None,
                           "wireless_runs": 0})
            continue
        schedule = warmups[1:] if density in initial else warmups
        for warm in schedule:
            flags, missing = pair(density, warm, "stability_retry" if warm != first else "grid_search")
            if missing:
                return result("candidate_runs_required", next_runs=missing)
            if flags["stable"]:
                break
        if all(accepted.values()):
            return finalize()
    return result("grid_exhausted", selected={k: min(v) if v else None for k, v in accepted.items()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    measure = sub.add_parser("measure", help="Read a single complete E0 attempt")
    measure.add_argument("attempt", type=Path)
    select = sub.add_parser("decide", help="Reassess attempts and report the exact next candidates")
    select.add_argument("--config", type=Path, required=True)
    select.add_argument("--synthetic", action="store_true", help="Permit synthetic evidence only; never releases E1")
    select.add_argument("--geometry-exclusions", type=Path, nargs="+", default=[],
                        help="Actual per-seed preregistration rejection JSONs; use -- before positional attempt paths")
    select.add_argument("attempt", type=Path, nargs="*")
    for child in (measure, select):
        child.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "measure":
            answer = evaluate(args.attempt)
        else:
            answer = decide(read_json(args.config), [evaluate(p) for p in args.attempt],
                            "synthetic" if args.synthetic else "simulation", args.geometry_exclusions)
        code = 0 if args.action == "measure" or answer["status"] == "selected" else 40
    except (ValueError, KeyError, TypeError, OSError) as exc:
        answer, code = {"status": "invalid", "reason": str(exc)}, 30
    output = json.dumps(answer, ensure_ascii=False, allow_nan=False, indent=2)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
    return code


if __name__ == "__main__":
    sys.exit(main())
