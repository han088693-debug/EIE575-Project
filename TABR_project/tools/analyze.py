#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Audit one TABR smoke run. Compatible with Python 2.7 and Python 3."""
from __future__ import print_function, unicode_literals

import argparse
import bisect
import csv
import glob
import hashlib
import io
import json
import math
import os
import shutil
import sys
import tempfile

PY2 = sys.version_info[0] == 2
EPS = 1e-7
PERIOD = 0.1
DEADLINE = 0.8


def read_csv(path, columns):
    stream = open(path, "rb") if PY2 else io.open(path, "r", encoding="utf-8-sig", newline="")
    try:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        if PY2:
            fields = [field.decode("utf-8-sig") for field in fields]
        missing = set(columns) - set(fields)
        if missing:
            raise ValueError("%s lacks columns: %s" % (os.path.basename(path), sorted(missing)))
        rows = []
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("%s contains a malformed CSV row" % os.path.basename(path))
            if PY2:
                row = dict((key.decode("utf-8-sig"), value.decode("utf-8")) for key, value in row.items())
            rows.append(row)
        return rows
    finally:
        stream.close()


def number(row, key):
    value = float(row[key])
    if math.isnan(value) or math.isinf(value):
        raise ValueError("Non-finite value in %s" % key)
    return value


def flag(row, key):
    if row[key] not in ("0", "1"):
        raise ValueError("Expected 0 or 1 in %s" % key)
    return row[key] == "1"


def close(left, right):
    return abs(left - right) <= EPS


def busy_integral(rows):
    times, areas, states = [], [], []
    for row in rows:
        stamp = number(row, "transition_time")
        state = flag(row, "busy_state")
        if times and stamp < times[-1] - EPS:
            raise ValueError("busy_audit timestamps are not monotonic")
        area = areas[-1] + (stamp - times[-1]) * states[-1] if times else 0.0
        times.append(stamp)
        areas.append(area)
        states.append(state)

    def at(stamp):
        index = bisect.bisect_right(times, stamp + 1e-12) - 1
        if index < 0:
            raise ValueError("busy_audit does not cover a control window")
        return areas[index] + (stamp - times[index]) * states[index]
    return at


def valid_state(row):
    return flag(row, "state_valid") and number(row, "t_gen") <= number(row, "t_rx") + EPS


def valid_event(row):
    return (valid_state(row) and flag(row, "event_valid")
            and number(row, "event_id") >= 0
            and number(row, "event_detect_time") >= 0
            and number(row, "t_gen") + EPS >= number(row, "event_detect_time")
            and number(row, "t_rx") <= number(row, "event_valid_until") + EPS)


def truth_hash(rows):
    # Exclude only run_id; preserve all trajectory data and deterministic ordering.
    normalized = [dict((key, value) for key, value in row.items() if key != "run_id") for row in rows]
    normalized.sort(key=lambda row: (row["vehicle_id"], float(row["t"])))
    encoded = json.dumps(normalized, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def analyze(directory):
    checks, vehicle_metrics, events = [], {}, []

    def check(name, passed, detail):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    planned = read_csv(os.path.join(directory, "event_log.csv"), [
        "run_id", "planned_event_id", "sender_id", "receiver_id", "planned_time",
        "t0", "deadline_s", "deadline_at", "obs_end", "event_status", "exclusion_reason"])
    truth = read_csv(os.path.join(directory, "scenario_truth.csv"), [
        "run_id", "t", "vehicle_id", "position_m", "speed_mps", "accel_mps2"])
    observed_run_ids = set(row["run_id"] for row in planned + truth)
    check("planned_event_present", bool(planned), "Keep every planned event, including failed scenarios.")
    check("scenario_truth_present", bool(truth), "%d trajectory samples" % len(truth))
    controls, receptions, states = {}, {}, {}
    paths = sorted(glob.glob(os.path.join(directory, "control_log_*.csv")))
    check("vehicle_logs_present", bool(paths), "%d vehicles" % len(paths))
    for path in paths:
        vehicle = os.path.basename(path)[len("control_log_"):-4]
        control = read_csv(path, ["run_id", "vehicle_id", "tick_time", "window_start", "window_end",
                                  "busy_duration_s", "C"])
        tx = read_csv(os.path.join(directory, "tx_log_%s.csv" % vehicle),
                      ["run_id", "sender_id", "seq", "t_gen", "t_submit", "message_bits"])
        busy = read_csv(os.path.join(directory, "busy_audit_%s.csv" % vehicle),
                        ["run_id", "vehicle_id", "transition_time", "busy_state"])
        rx = read_csv(os.path.join(directory, "rx_log_%s.csv" % vehicle),
                      ["run_id", "receiver_id", "sender_id", "seq", "t_rx", "t_gen", "state_valid",
                       "event_valid", "event_id", "event_detect_time", "event_valid_until"])
        state = read_csv(os.path.join(directory, "state_log_%s.csv" % vehicle),
                         ["run_id", "t", "receiver_id", "sender_id", "known", "state_source",
                          "last_seq", "last_state_gen_time", "p_last_m", "v_last_mps", "p_hat_m", "Age_s"])
        controls[vehicle], receptions[vehicle], states[vehicle] = control, rx, state
        observed_run_ids.update(row["run_id"] for row in control + tx + busy + rx + state)
        check(vehicle + ".required_rows", bool(control and tx and busy),
              "control=%d tx=%d busy=%d" % (len(control), len(tx), len(busy)))
        if not control or not busy:
            continue
        ticks = [number(row, "tick_time") for row in control]
        windows_ok = all(close(number(row, "window_end"), number(row, "tick_time"))
                         and close(number(row, "window_end") - number(row, "window_start"), PERIOD)
                         for row in control)
        cadence_ok = all(close(right - left, PERIOD) for left, right in zip(ticks, ticks[1:]))
        continuity_ok = all(close(number(a, "window_end"), number(b, "window_start"))
                            for a, b in zip(control, control[1:]))
        check(vehicle + ".control_period_0_1", windows_ok and cadence_ok and continuity_ok,
              "%d control ticks" % len(ticks))
        integral = busy_integral(busy)
        max_formula_error, max_audit_error = 0.0, 0.0
        values_in_range = True
        for row in control:
            start, end = number(row, "window_start"), number(row, "window_end")
            duration, cbr = number(row, "busy_duration_s"), number(row, "C")
            if end <= start:
                raise ValueError("Control window has non-positive duration")
            max_formula_error = max(max_formula_error, abs(cbr - duration / (end - start)))
            max_audit_error = max(max_audit_error, abs(cbr - (integral(end) - integral(start)) / (end - start)))
            values_in_range = values_in_range and -EPS <= cbr <= 1 + EPS and -EPS <= duration <= end - start + EPS
        check(vehicle + ".C_busy_duration_over_window", values_in_range and max_formula_error <= EPS,
              "max_abs_error=%g" % max_formula_error)
        check(vehicle + ".C_independent_busy_audit", max_audit_error <= EPS,
              "max_abs_error=%g" % max_audit_error)
        submit = [number(row, "t_submit") for row in tx]
        gaps = [right - left for left, right in zip(submit, submit[1:])]
        check(vehicle + ".tx_gap_at_least_0_1", bool(tx) and all(gap + EPS >= PERIOD for gap in gaps),
              "min_gap_s=%s" % (min(gaps) if gaps else "not_applicable_one_or_zero_tx"))
        check(vehicle + ".message_bits_2000", bool(tx) and all(number(row, "message_bits") == 2000 for row in tx),
              "%d transmissions" % len(tx))
        check(vehicle + ".log_identity",
              all(row["vehicle_id"] == vehicle for row in control + busy)
              and all(row["sender_id"] == vehicle for row in tx)
              and all(row["receiver_id"] == vehicle for row in rx + state),
              "Vehicle identity matches the filename.")
        vehicle_metrics[vehicle] = {"control_ticks": len(control), "transmissions": len(tx),
                                   "min_tx_interval_s": min(gaps) if gaps else None,
                                   "max_C_formula_error": max_formula_error,
                                   "max_C_busy_audit_error": max_audit_error}

    for planned_event in planned:
        event = dict(planned_event)
        event.update({"D_info_s": None, "D_update_s": None, "first_event_rx": None,
                      "first_update_rx": None, "event_id": None, "t_detect": None,
                      "no_receive": True, "deadline_miss": True, "analysis_status": "invalid_scenario"})
        events.append(event)
        prefix = "event.%s" % planned_event["planned_event_id"]
        sender, receiver = planned_event["sender_id"], planned_event["receiver_id"]
        scenario_ok = planned_event["event_status"] == "occurred" and not planned_event["exclusion_reason"]
        check(prefix + ".scenario_occurred", scenario_ok,
              "status=%s reason=%s" % (planned_event["event_status"], planned_event["exclusion_reason"]))
        if not scenario_ok or not planned_event["t0"] or not planned_event["obs_end"]:
            check(prefix + ".event_timing_available", False, "Cannot accept incomplete or failed scenario.")
            continue
        t0, obs_end = number(planned_event, "t0"), number(planned_event, "obs_end")
        deadline = number(planned_event, "deadline_s")
        timing_ok = (close(deadline, DEADLINE) and obs_end + EPS >= t0 + deadline
                     and planned_event["deadline_at"] and close(number(planned_event, "deadline_at"), t0 + deadline))
        check(prefix + ".deadline_0_8", timing_ok, "deadline_s=%g" % deadline)
        check(prefix + ".pair_logs_present", sender in controls and receiver in controls,
              "%s -> %s" % (sender, receiver))
        pair_truth = {}
        for vehicle in (sender, receiver):
            samples = [row for row in truth if row["vehicle_id"] == vehicle]
            pair_truth[vehicle] = samples
            stamps = [number(row, "t") for row in samples]
            complete = bool(stamps) and min(stamps) <= t0 + EPS and max(stamps) + EPS >= obs_end
            regular = all(close(right - left, PERIOD) for left, right in zip(stamps, stamps[1:]))
            check(prefix + "." + vehicle + ".observation_complete", complete and regular,
                  "samples=%d last_t=%s required_obs_end=%g" % (len(samples), max(stamps) if stamps else None, obs_end))
            vehicle_controls = controls.get(vehicle, [])
            control_complete = (bool(vehicle_controls)
                                and number(vehicle_controls[0], "window_start") <= t0 + EPS
                                and number(vehicle_controls[-1], "window_end") + PERIOD + EPS >= obs_end)
            check(prefix + "." + vehicle + ".control_observation_complete", control_complete,
                  "Controls must continue through the observation window, allowing one unfinished final tick.")
        lead_samples = pair_truth[sender]
        actual_drop = any(close(number(after, "t"), t0)
                          and number(after, "speed_mps") < number(before, "speed_mps") - EPS
                          and after["accel_mps2"] and number(after, "accel_mps2") < -EPS
                          for before, after in zip(lead_samples, lead_samples[1:]))
        check(prefix + ".actual_speed_drop_at_t0", actual_drop, "Independent scenario_truth speed difference.")
        detection = read_csv(os.path.join(directory, "detection_log_%s.csv" % sender),
                             ["run_id", "sender_id", "event_id", "t_detect", "event_valid_until"])
        observed_run_ids.update(row["run_id"] for row in detection)
        candidates = [row for row in detection if row["sender_id"] == sender
                      and t0 - EPS <= number(row, "t_detect") <= obs_end + EPS
                      and number(row, "event_id") >= 0
                      and number(row, "event_valid_until") >= number(row, "t_detect")]
        candidates.sort(key=lambda row: number(row, "t_detect"))
        check(prefix + ".braking_detected", bool(candidates), "%d matching detector records" % len(candidates))
        detection_row = candidates[0] if candidates else None
        if detection_row:
            event["event_id"] = detection_row["event_id"]
            event["t_detect"] = number(detection_row, "t_detect")
        pair_rx = [row for row in receptions.get(receiver, []) if row["sender_id"] == sender]
        valid_updates = [row for row in pair_rx if valid_state(row)
                         and number(row, "t_gen") + EPS >= t0
                         and t0 - EPS <= number(row, "t_rx") <= obs_end + EPS]
        valid_events = [row for row in pair_rx if detection_row and valid_event(row)
                        and row["event_id"] == detection_row["event_id"]
                        and close(number(row, "event_detect_time"), number(detection_row, "t_detect"))
                        and t0 - EPS <= number(row, "t_rx") <= obs_end + EPS]
        if valid_updates:
            event["first_update_rx"] = min(number(row, "t_rx") for row in valid_updates)
            event["D_update_s"] = max(0.0, event["first_update_rx"] - t0)
        if valid_events:
            event["first_event_rx"] = min(number(row, "t_rx") for row in valid_events)
            event["D_info_s"] = max(0.0, event["first_event_rx"] - t0)
            event["no_receive"] = False
            event["deadline_miss"] = event["D_info_s"] > deadline + EPS
        event["analysis_status"] = "no_receive" if event["no_receive"] else ("deadline_miss" if event["deadline_miss"] else "received_on_time")
        check(prefix + ".valid_event_received", not event["no_receive"], event["analysis_status"])
        check(prefix + ".information_deadline", not event["deadline_miss"],
              "D_info_s=%s deadline_s=%g" % (event["D_info_s"], deadline))
        check(prefix + ".post_event_state_update_received", event["D_update_s"] is not None,
              "D_update_s=%s; requires t_gen >= t0" % event["D_update_s"])

        target_states = [row for row in states.get(receiver, []) if row["sender_id"] == sender]
        target_controls = controls.get(receiver, [])
        valid_rx_times = sorted(number(row, "t_rx") for row in pair_rx if valid_state(row))
        first_valid = valid_rx_times[0] if valid_rx_times else None
        state_ticks = [number(row, "t") for row in target_states]
        expected_ticks = [number(row, "tick_time") for row in target_controls]
        coverage = (len(state_ticks) == len(expected_ticks)
                    and all(close(left, right) for left, right in zip(state_ticks, expected_ticks)))
        unknown_count, unknown_expected, values_ok = 0, 0, True
        unknown_fields = ("last_seq", "last_state_gen_time", "p_last_m", "v_last_mps", "p_hat_m", "Age_s")
        for row in target_states:
            stamp, known = number(row, "t"), flag(row, "known")
            before_reception = first_valid is None or stamp < first_valid - EPS
            if before_reception:
                unknown_expected += 1
                values_ok = values_ok and not known
            if not known:
                unknown_count += 1
                values_ok = values_ok and row["state_source"] == "unknown" and all(row[key] == "" for key in unknown_fields)
            else:
                values_ok = values_ok and row["state_source"] == "received" and all(row[key] != "" for key in unknown_fields)
                if values_ok:
                    values_ok = (number(row, "last_state_gen_time") <= stamp + EPS
                                 and close(number(row, "Age_s"), stamp - number(row, "last_state_gen_time")))
            if first_valid is not None and stamp > first_valid + EPS:
                values_ok = values_ok and known
        check(prefix + ".target_state_tick_coverage", coverage and bool(target_controls),
              "state_ticks=%d control_ticks=%d" % (len(state_ticks), len(expected_ticks)))
        check(prefix + ".unknown_state_is_explicit", values_ok and coverage,
              "unknown_rows=%d required_before_first_rx=%d" % (unknown_count, unknown_expected))
        event["unknown_state_rows"] = unknown_count
        event["pre_receive_control_ticks"] = unknown_expected

    run_ids = sorted(observed_run_ids)
    check("single_run_id", len(run_ids) == 1, "run_ids=%s" % run_ids)
    return {"schema_version": 1, "passed": bool(checks) and all(item["passed"] for item in checks),
            "checks": checks, "planned_events": planned,
            "metrics": {"vehicle_count": len(controls), "vehicles": vehicle_metrics, "events": events,
                        "scenario_truth_sha256_without_run_id": truth_hash(truth), "run_ids": run_ids},
            "scope": "Smoke validation only; no significance test or claimed scheme ranking.",
            "timing_note": "t0 is the first sampled true speed decrease; no_receive delays remain null."}


def write_csv(path, fields, rows):
    stream = open(path, "wb") if PY2 else io.open(path, "w", encoding="utf-8", newline="")
    try:
        if PY2:
            fields = [field.encode("utf-8") for field in fields]
            rows = [dict((key.encode("utf-8"), value.encode("utf-8") if isinstance(value, unicode) else value)
                         for key, value in row.items()) for row in rows]
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    finally:
        stream.close()


def save_summary(directory, summary):
    text = json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True)
    if PY2 and isinstance(text, str):
        text = text.decode("ascii")
    with io.open(os.path.join(directory, "summary.json"), "w", encoding="utf-8") as stream:
        stream.write(text + "\n")
    events = summary.get("metrics", {}).get("events", [])
    fields = ["planned_event_id", "sender_id", "receiver_id", "planned_time", "t0", "event_status",
              "exclusion_reason", "event_id", "t_detect", "deadline_s", "obs_end", "D_info_s",
              "D_update_s", "first_event_rx", "first_update_rx", "no_receive", "deadline_miss",
              "analysis_status", "unknown_state_rows", "pre_receive_control_ticks"]
    write_csv(os.path.join(directory, "event_summary.csv"), fields, events)


def self_test():
    root = tempfile.mkdtemp(prefix="tabr-analyze-")
    try:
        def put(name, header, lines):
            with io.open(os.path.join(root, name + ".csv"), "w", encoding="utf-8") as stream:
                stream.write(header + "\n" + "\n".join(lines) + "\n")
        ticks = [index / 10.0 for index in range(1, 13)]
        put("event_log", "run_id,planned_event_id,sender_id,receiver_id,planned_time,t0,deadline_s,deadline_at,obs_end,event_status,exclusion_reason",
            ["test,lead-brake-1,lead,follow,0.1,0.2,0.8,1.0,1.2,occurred,"])
        truth = []
        for vehicle in ("lead", "follow"):
            for index in range(13):
                stamp = index / 10.0
                speed = 9 if vehicle == "lead" and index >= 2 else 10
                accel = -10 if vehicle == "lead" and index == 2 else 0
                truth.append("test,%s,%s,%s,%s,%s" % (stamp, vehicle, stamp * 10, speed, accel))
            put("control_log_" + vehicle, "run_id,vehicle_id,tick_time,window_start,window_end,busy_duration_s,C",
                ["test,%s,%s,%s,%s,%s,%s" % (vehicle, stamp, stamp - 0.1, stamp, 0.04 if index == 0 else 0, 0.4 if index == 0 else 0)
                 for index, stamp in enumerate(ticks)])
            put("tx_log_" + vehicle, "run_id,sender_id,seq,t_gen,t_submit,message_bits",
                ["test,%s,%s,%s,%s,2000" % (vehicle, index, stamp, stamp) for index, stamp in enumerate((0.1, 0.3, 0.5))])
            put("busy_audit_" + vehicle, "run_id,vehicle_id,transition_time,busy_state",
                ["test,%s,0,0" % vehicle, "test,%s,0.02,1" % vehicle, "test,%s,0.06,0" % vehicle])
            put("detection_log_" + vehicle, "run_id,sender_id,event_id,t_detect,event_valid_until",
                ["test,lead,0,0.2,2.2"] if vehicle == "lead" else [])
        put("scenario_truth", "run_id,t,vehicle_id,position_m,speed_mps,accel_mps2", truth)
        rx_header = "run_id,receiver_id,sender_id,seq,t_rx,t_gen,state_valid,event_valid,event_id,event_detect_time,event_valid_until"
        state_header = "run_id,t,receiver_id,sender_id,known,state_source,last_seq,last_state_gen_time,p_last_m,v_last_mps,p_hat_m,Age_s"
        put("rx_log_lead", rx_header, [])
        put("state_log_lead", state_header, [])
        for receives in (True, False):
            put("rx_log_follow", rx_header,
                ["test,follow,lead,0,0.15,0.1,1,0,-1,-1,-1",
                 "test,follow,lead,1,0.35,0.3,1,1,0,0.2,2.2"] if receives else [])
            rows = []
            for stamp in ticks:
                if not receives or stamp < 0.15:
                    rows.append("test,%s,follow,lead,0,unknown,,,,,," % stamp)
                else:
                    generated, seq = (0.3, 1) if stamp > 0.35 else (0.1, 0)
                    rows.append("test,%s,follow,lead,1,received,%s,%s,1,10,2,%s" % (stamp, seq, generated, stamp - generated))
            put("state_log_follow", state_header, rows)
            result = analyze(root)
            save_summary(root, result)
            with io.open(os.path.join(root, "summary.json"), "r", encoding="utf-8") as stream:
                result = json.load(stream)
            event = result["metrics"]["events"][0]
            assert result["passed"] == receives, result["checks"]
            assert event["no_receive"] != receives
            assert event["deadline_miss"] != receives
            if receives:
                assert close(event["D_info_s"], 0.15) and close(event["D_update_s"], 0.15)
            else:
                assert event["D_info_s"] is None and event["D_update_s"] is None
                assert event["unknown_state_rows"] == len(ticks)
        print("SELF_TEST_PASS: reception and no_receive fixtures")
    finally:
        # Remove only the fixture directory created by this function.
        resolved = os.path.realpath(root)
        temporary_parent = os.path.realpath(tempfile.gettempdir())
        if os.path.dirname(resolved) == temporary_parent and os.path.basename(resolved).startswith("tabr-analyze-"):
            shutil.rmtree(resolved)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result_dir", nargs="?", help="One simulation result directory")
    parser.add_argument("--self-test", action="store_true", help="Check reception and no_receive fixtures")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if not args.result_dir:
        parser.error("result_dir is required unless --self-test is given")
    directory = args.result_dir
    if PY2 and isinstance(directory, str):
        directory = directory.decode(sys.getfilesystemencoding() or "mbcs")
    directory = os.path.abspath(directory)
    if not os.path.isdir(directory):
        parser.error("result_dir does not exist")
    try:
        summary = analyze(directory)
    except (IOError, OSError, ValueError, KeyError) as error:
        try:
            planned = read_csv(os.path.join(directory, "event_log.csv"), [])
        except (IOError, OSError, ValueError, KeyError):
            planned = []
        summary = {"schema_version": 1, "passed": False,
                   "checks": [{"name": "read_and_validate_logs", "passed": False, "detail": repr(error)}],
                   "planned_events": planned, "metrics": {},
                   "error": "Missing, malformed, or invalid log data."}
    save_summary(directory, summary)
    failed = [check["name"] for check in summary["checks"] if not check["passed"]]
    print("PASS" if summary["passed"] else "FAIL")
    if failed:
        print("Failed checks: " + ", ".join(failed))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
