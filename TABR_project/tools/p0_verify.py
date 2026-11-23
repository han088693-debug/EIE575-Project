"""Recheck the fixed 30-second v0.2 seed-0 baseline, or inventory its dependencies.

python -B tools/p0_verify.py --results PATH --output report.json
python -B tools/p0_verify.py --inventory --output environment.json
"""
import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys

import analyze

ROOT = Path(__file__).resolve().parents[1]
SCHEMES = ("G0", "G1", "G2", "G3")
VEHICLES = ("lead", "follow")
BASELINE = ROOT / "verification/g1_speed_revision_20261021"
COURSE = Path("E:/港理课程")
ARCHIVE = COURSE / "课程文件/eie575/project/2026-10-21_基础代码/EIE575_TABR_Starter"
TOOLS = Path("E:/EIE575_Tools")
PRIMARY = (
    "src/TabrApp.cc", "src/TabrApp.h", "src/TabrApp.ned", "src/TabrCore.h",
    "src/TabrBeacon.msg", "src/BrakeScenario.cc", "src/BrakeScenario.h",
    "src/BrakeScenario.ned", "src/ResearchHighway.ned", "omnetpp.ini",
    "radio.xml", "tools/manage.py", "tools/analyze.py",
)


def json_file(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def csv_file(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
        if not reader.fieldnames or any(None in row or None in row.values() for row in rows):
            raise ValueError(f"Malformed CSV: {path}")
        return reader.fieldnames, rows


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def clip(value, low, high):
    return max(low, min(value, high))


def indexed_runs(path):
    runs = {}
    for meta in path.rglob("run.json"):
        data = json_file(meta)
        if data.get("scheme") not in SCHEMES or data.get("seed") != 0:
            raise ValueError(f"Unexpected job in P0 input: {meta}")
        key = data["scheme"]
        if key in runs:
            raise ValueError(f"Duplicate {key} seed 0 at {meta} and {runs[key]}")
        runs[key] = meta.parent
    if set(runs) != set(SCHEMES):
        raise ValueError(f"Expected {SCHEMES}, found {sorted(runs)} under {path}")
    return runs


def compare_trace(reference, current):
    ref_header, old = csv_file(reference)
    new_header, new = csv_file(current)
    if ref_header != new_header or len(old) != len(new):
        raise ValueError(f"{current.name}: schema or row count changed ({len(old)} -> {len(new)})")
    keys = [key for key in ref_header if key != "run_id"]
    for line, (left, right) in enumerate(zip(old, new), start=2):
        for key in keys:
            if left[key] == right[key]:
                continue
            try:
                same = math.isclose(float(left[key]), float(right[key]), rel_tol=1e-12, abs_tol=1e-10)
            except ValueError:
                same = False
            if not same:
                raise ValueError(f"{current.name}:{line} {key} changed from {left[key]} to {right[key]}")
    return len(new)


def audit_controller(directory, scheme, vehicle):
    _, rows = csv_file(directory / f"control_log_{vehicle}.csv")
    errors = dict.fromkeys(("Cbar", "b", "W", "U", "r"), 0.0)
    smoothed, base = None, 2.0
    for row in rows:
        cbr = float(row["C"])
        smoothed = cbr if smoothed is None else 0.8 * smoothed + 0.2 * cbr
        error = 0.6 - smoothed
        if abs(error) > 0.03 + 1e-12:
            base = clip(base + 0.5 * error, 1 / 3, 10)
        brake, score, speed = int(row["B"]), float(row["S"]), float(row["speed_mps"])
        weight = {"G0": 1, "G1": clip(speed / (33 / 3.6), 1, 3),
                  "G2": 1 + 2 * brake, "G3": 1 + 2 * max(brake, score)}[scheme]
        expected = {"Cbar": smoothed, "b": base, "W": weight,
                    "U": (weight - 1) / 2, "r": clip(base * weight, 1, 10)}
        for key, value in expected.items():
            actual = float(row[key])
            if not math.isfinite(actual) or not math.isfinite(value):
                raise ValueError(f"{vehicle}@{row['tick_time']} {key} nonfinite")
            errors[key] = max(errors[key], abs(actual - value))
    if not rows or max(errors.values()) > 1e-7:
        raise ValueError(f"{scheme} {vehicle}: controller mismatch, {errors}")
    _, tx = csv_file(directory / f"tx_log_{vehicle}.csv")
    stamps = [float(row["t_submit"]) for row in tx]
    gaps = [right - left for left, right in zip(stamps, stamps[1:])]
    if not gaps or any(not 0.1 - 1e-9 <= gap <= 1 + 1e-9 for gap in gaps):
        raise ValueError(f"{scheme} {vehicle}: submission interval out of bounds")
    return {"control_rows": len(rows), "max_absolute_error": errors,
            "transmissions": len(tx), "min_interval_s": min(gaps), "max_interval_s": max(gaps)}


def audit_run(current, baseline, scheme):
    smoke = analyze.analyze(str(current))
    failed = [check["name"] for check in smoke["checks"] if not check["passed"]]
    logged = json_file(current / "summary.json")
    if failed or not logged.get("passed") or not smoke["passed"]:
        raise ValueError(f"{scheme}: smoke validation failed: {failed}")
    if smoke["metrics"]["run_ids"] != [json_file(current / "run.json")["run_id"]]:
        raise ValueError(f"{scheme}: CSV and run metadata identities differ")
    traces = {p.name: compare_trace(p, current / p.name) for p in sorted(baseline.glob("*.csv"))
              if p.name != "event_summary.csv"}
    event_keys = ("planned_event_id", "analysis_status", "D_info_s", "D_update_s",
                  "unknown_state_rows", "pre_receive_control_ticks")
    event = smoke["metrics"]["events"][0]
    expected = json_file(baseline / "summary.json")["metrics"]["events"][0]
    if any(event[key] != expected[key] for key in event_keys):
        raise ValueError(f"{scheme}: derived event results differ")
    vehicles = {vehicle: audit_controller(current, scheme, vehicle) for vehicle in VEHICLES}
    for vehicle, recomputed in smoke["metrics"]["vehicles"].items():
        vehicles[vehicle]["max_busy_integral_error"] = recomputed["max_C_busy_audit_error"]
    return {"smoke_checks": len(smoke["checks"]), "compared_raw_csv_rows": traces,
            "trajectory_sha256": smoke["metrics"]["scenario_truth_sha256_without_run_id"],
            "vehicles": vehicles, "event": {key: event[key] for key in event_keys}}


def audit(results, baseline):
    current, history = indexed_runs(results), indexed_runs(baseline)
    runs = {scheme: audit_run(current[scheme], history[scheme], scheme) for scheme in SCHEMES}
    digests = {run["trajectory_sha256"] for run in runs.values()}
    if len(digests) != 1:
        raise ValueError(f"Unpaired vehicle trajectories: {sorted(digests)}")
    return {"passed": True, "mode": "P0-v0.2-historical-regression", "seed": 0,
            "results": str(results.resolve()), "reference": str(baseline.resolve()),
            "independent_new_attempt": results.resolve() != baseline.resolve(),
            "control_rows": sum(v["control_rows"] for run in runs.values() for v in run["vehicles"].values()),
            "runs": runs}


def executable_check(command, env=None):
    completed = subprocess.run([str(item) for item in command], capture_output=True, env=env,
                               timeout=30, encoding="utf-8", errors="replace")
    detail = (completed.stdout + completed.stderr).strip()
    if completed.returncode:
        raise ValueError(f"{command[0]} exited {completed.returncode}: {detail[:320]}")
    return detail


def inventory(args):
    source, origin, dep = args.source, args.old_runtime, args.tools_root
    source_names = list(PRIMARY) + ["src/package.ned", "run.ps1", "batch.ps1", "tools/batch.py",
                                   "tests/core_test.cpp", "tests/run_core_tests.ps1"]
    source_names += [p.relative_to(source).as_posix() for p in sorted((source / "scenarios").glob("*"))
                     if p.suffix in {".xml", ".cfg"}]
    source_hash = {name: {"course": sha(source / name), "old_runtime": sha(origin / name),
                          "work": sha(ROOT / name)} for name in source_names}
    changed = [k for k, v in source_hash.items() if v["course"] != v["old_runtime"]]
    if changed:
        raise ValueError(f"Archived course and historic runtime inputs differ: {changed}")
    omnet, veins, sumo = dep / "omnetpp-5.6.2", dep / "veins-5.0", dep / "sumo-1.6.0"
    mingw = omnet / "tools/win64/mingw64/bin"
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([str(omnet / "bin"), str(mingw), str(veins / "src"),
                                    str(sumo / "bin"), env.get("PATH", "")])
    tool_files = [omnet / "Version", omnet / "bin/opp_run.exe", omnet / "bin/opp_makemake",
                  omnet / "tools/win64/usr/bin/bash.exe", mingw / "clang++.exe",
                  mingw / "python2.exe", mingw / "ld.exe", veins / "sumo-launchd.py", veins / "src/veins/veins.h",
                  veins / "src/libveins.dll", sumo / "bin/sumo.exe", sumo / "bin/netconvert.exe"]
    tool_files += sorted((omnet / "lib").glob("libopp*.a"))
    ve_candidates = sorted((veins / "src").glob("*veins*.a")) + sorted((veins / "src").glob("*veins*.lib"))
    tool_files += ve_candidates
    py_probe = ("import sys, json, importlib\nr={'version':sys.version, 'imports':{}}\n"
                "for n in ('numpy','matplotlib','scipy','pandas'):\n"
                " try: r['imports'][n]=importlib.import_module(n).__version__\n"
                " except Exception as error: r['imports'][n]={'error':str(error)}\n"
                "print(json.dumps(r))")
    py = {str(path): json.loads(executable_check([path, "-B", "-c", py_probe], env))
          for path in dict.fromkeys((Path(sys.executable), args.analysis_python))}
    return {"passed": True, "mode": "P0-baseline-and-toolchain", "primary13": list(PRIMARY),
            "source_dir": str(source), "old_runtime_dir": str(origin), "work_dir": str(ROOT),
            "sha256_by_relative_file": source_hash, "work_changed": [k for k, v in source_hash.items()
            if v["course"] != v["work"]], "tool_sha256": {str(p): sha(p) for p in tool_files},
            "veins_link_directory": str(veins / "src"),
            "veins_separate_import_archive_candidates": [str(p) for p in ve_candidates],
            "veins_version": executable_check([sys.executable, "-B", veins / "print-veins-version"], env),
            "omnet_version": (omnet / "Version").read_text().strip(),
            "clang": executable_check([mingw / "clang++.exe", "--version"], env).splitlines()[0],
            "sumo": executable_check([sumo / "bin/sumo.exe", "--version"], env).splitlines()[0],
            "launcher_python": executable_check([mingw / "python2.exe", "-V"], env),
            "analysis_python": str(args.analysis_python), "interpreters": py}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--results", type=Path)
    mode.add_argument("--inventory", action="store_true")
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument("--source", type=Path, default=ARCHIVE)
    parser.add_argument("--old-runtime", type=Path, default=TOOLS / "workspace/EIE575_TABR_Starter")
    parser.add_argument("--tools-root", type=Path, default=TOOLS)
    parser.add_argument("--analysis-python", type=Path, default=COURSE / ".venvs/eie546-lab1/Scripts/python.exe")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = inventory(args) if args.inventory else audit(args.results, args.baseline)
    except (OSError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired) as exc:
        report = {"passed": False, "error": str(exc)}
    args.output.parent.mkdir(exist_ok=True, parents=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "output": str(args.output),
                      "error": report.get("error")}, ensure_ascii=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
