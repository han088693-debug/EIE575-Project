"""Summarise beta-sweep probe attempts into one table.

Usage:
    python tools/bsweep.py --root E:/Temp/bsweep --out E:/Temp/bsweep/summary.csv

Reads every attempt-* directory under --root, then for each one reports
config beta, validity, D_info (both onset endpoints), event-window rate /
base rate / multiplier / CBR for the target pair, and transport totals.
"""
import argparse
import csv
import io
import json
import os
import statistics
from pathlib import Path


def read_control(path, vehicle, t0, t1):
    rows = []
    with io.open(path, encoding="utf-8", errors="replace", newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("vehicle_id") != vehicle:
                continue
            try:
                t = float(row["tick_time"])
            except (TypeError, ValueError):
                continue
            if t0 <= t <= t1:
                rows.append(row)
    return rows


def stats(rows):
    if not rows:
        return {}
    out = {}
    for key in ("Cbar", "b", "r", "W"):
        vals = [float(r[key]) for r in rows if r.get(key) not in (None, "")]
        if vals:
            out[key] = statistics.fmean(vals)
    rates = [float(r["r"]) for r in rows if r.get("r") not in (None, "")]
    if rates:
        out["r_max"] = max(rates)
        out["r_sat_frac"] = sum(1 for x in rates if x >= 9.99) / len(rates)
    out["n"] = len(rows)
    return out


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="E:/Temp/bsweep")
    ap.add_argument("--out", default="E:/Temp/bsweep/summary.csv")
    return ap.parse_args()


def main():
    args = parse_args()
    root = Path(args.root)
    records = []
    for attempt in sorted(root.rglob("attempt-*")):
        metrics_path = attempt / "metrics.json"
        snapshot_path = attempt / "config_snapshot.json"
        if not metrics_path.exists() or not snapshot_path.exists():
            continue
        cfg = json.load(io.open(snapshot_path, encoding="utf-8"))
        metrics = json.load(io.open(metrics_path, encoding="utf-8"))
        events = metrics.get("events") or []
        event = events[0] if events else {}
        t0 = event.get("t0_s")
        post = float(cfg["timing"]["post_event_s"])
        lead = follow = {}
        if t0 is not None:
            ctl = attempt / "logs" / "control_log.csv"
            if ctl.exists():
                lead = stats(read_control(ctl, "lead", t0, t0 + post))
                follow = stats(read_control(ctl, "follow", t0, t0 + post))
        rec = {
            "dir": str(attempt.relative_to(root)),
            "beta": cfg["control"]["beta"],
            "scheme": metrics.get("scheme"),
            "seed": metrics.get("seed"),
            "validity": metrics.get("data_validity"),
            "duration_s": cfg["timing"]["duration_s"],
            "length_m": cfg["geometry"]["length_m"],
            "vehicles": metrics.get("transport", {}).get("vehicles"),
            "t0_s": t0,
            "t_detect_s": event.get("t_detect_s"),
            "D_info_ms": None if event.get("D_info_s") is None else round(event["D_info_s"] * 1000, 1),
            "D_info_lower_ms": None if (event.get("at_lower_endpoint") or {}).get("D_info_s") is None
            else round(event["at_lower_endpoint"]["D_info_s"] * 1000, 1),
            "delivered": event.get("delivered"),
            "deadline_miss": event.get("deadline_miss"),
            "lead_b": round(lead.get("b", 0), 3) if lead else None,
            "lead_W": round(lead.get("W", 0), 3) if lead else None,
            "lead_r": round(lead.get("r", 0), 3) if lead else None,
            "lead_r_max": round(lead.get("r_max", 0), 3) if lead else None,
            "lead_sat": round(lead.get("r_sat_frac", 0), 3) if lead else None,
            "lead_cbr": round(lead.get("Cbar", 0), 3) if lead else None,
            "follow_b": round(follow.get("b", 0), 3) if follow else None,
            "follow_r": round(follow.get("r", 0), 3) if follow else None,
            "all_tx": metrics.get("transport", {}).get("all_tx"),
            "all_rx": metrics.get("transport", {}).get("all_rx"),
        }
        records.append(rec)

    if not records:
        print("no attempts found under", root)
        return

    records.sort(key=lambda r: (r["beta"], r["seed"]))
    keys = list(records[0].keys())
    with io.open(args.out, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys)
        writer.writeheader()
        writer.writerows(records)
    print("wrote", args.out, "rows:", len(records))
    print()
    header = ("beta", "seed", "valid", "D_info_ms", "D_low_ms", "lead_b", "lead_W", "lead_r", "r_sat", "lead_cbr")
    print("%6s %5s %-7s %9s %9s %7s %6s %7s %6s %8s" % header)
    for r in records:
        print("%6.2f %5s %-7s %9s %9s %7s %6s %7s %6s %8s" % (
            r["beta"], r["seed"], r["validity"], r["D_info_ms"], r["D_info_lower_ms"],
            r["lead_b"], r["lead_W"], r["lead_r"], r["lead_sat"], r["lead_cbr"]))


if __name__ == "__main__":
    main()
