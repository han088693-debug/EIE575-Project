"""Independent motion validation and event-pair metrics for one formal TABR attempt."""
import csv
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import math
from pathlib import Path
from statistics import fmean


class Clock:
    def __init__(self, resolution):
        self.quantum = Decimal(str(resolution))
        if not self.quantum.is_finite() or self.quantum <= 0:
            raise ValueError("Invalid SimTime resolution")

    def tick(self, seconds):
        try:
            value = Decimal(str(seconds))
            if not value.is_finite():
                raise ValueError(f"Nonfinite time {seconds!r}")
            return int((value / self.quantum).to_integral_value(rounding=ROUND_HALF_EVEN))
        except InvalidOperation as exc:
            raise ValueError(f"Invalid time {seconds!r}") from exc

    def seconds(self, ticks):
        return None if ticks is None else float(self.quantum * ticks)


def read_rows(path, required, run_ids):
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields = reader.fieldnames or []
            missing = set(required.split()) - set(fields)
            if missing or len(fields) != len(set(fields)):
                raise ValueError(f"{path.name}: missing {sorted(missing)} or duplicate fields")
            for row in reader:
                if None in row or None in row.values():
                    raise ValueError(f"{path.name}:{reader.line_num} malformed CSV")
                if "run_id" in row:
                    run_ids.add(row["run_id"])
                    if "" in run_ids or len(run_ids) > 1:
                        raise ValueError(f"{path.name}:{reader.line_num} inconsistent run_id")
                yield row
    except (OSError, csv.Error) as exc:
        raise ValueError(f"Cannot read {path}: {exc}") from exc


def flag(value):
    if value in ("1", "true", "True"):
        return True
    if value in ("0", "false", "False"):
        return False
    raise ValueError(f"Invalid boolean {value!r}")


def finite(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Nonfinite motion value")
    return value


@dataclass(frozen=True)
class Plan:
    name: str
    sender: str
    command: int
    brake_duration: int
    deadline: int
    window: int


def registrations(inputs, cfg, clock, run_ids):
    timing = cfg["timing"]
    scenario = cfg["scenario"]
    plans, pairs = {}, {}
    for row in read_rows(inputs / "events.csv",
                         "planned_event_id sender_id planned_time_s target_speed_mps brake_duration_s deadline_s observation_s", run_ids):
        name, sender = row["planned_event_id"], row["sender_id"]
        event = Plan(name, sender, clock.tick(row["planned_time_s"]), clock.tick(row["brake_duration_s"]),
                     clock.tick(row["deadline_s"]), clock.tick(row["observation_s"]))
        if not name or not sender or name in plans:
            raise ValueError(f"Duplicate or empty physical registration {name!r}")
        if (event.deadline <= 0 or event.window < event.deadline or event.brake_duration <= 0
                or event.deadline != clock.tick(timing["deadline_s"]) or event.window != clock.tick(timing["observation_s"])
                or finite(row["target_speed_mps"]) != 0):
            raise ValueError(f"{name}: bad deadline, observation or braking registration")
        if event.command != clock.tick(timing["warmup_s"]):
            # The frozen core prescribes simultaneous target braking at the warmup boundary.
            raise ValueError(f"{name}: command time differs from the registered core warmup boundary")
        plans[name] = event
    primary = defaultdict(list)
    for row in read_rows(inputs / "pairs.csv", "pair_id sender_id receiver_id role planned_event_id", run_ids):
        key = (row["planned_event_id"], row["pair_id"])
        endpoints = row["sender_id"], row["receiver_id"]
        if key in pairs or not key[1] or not all(endpoints) or endpoints[0] == endpoints[1]:
            raise ValueError(f"Duplicate or invalid pair registration {key}")
        pairs[key] = endpoints
        if not key[0]:
            continue
        if key[0] not in plans or plans[key[0]].sender != endpoints[0] or row["role"] != "target":
            raise ValueError(f"Bad event pair association {key}")
        if any(receiver == endpoints[1] for _, receiver in primary[key[0]]):
            raise ValueError(f"{key}: repeated receiver for physical event")
        primary[key[0]].append((key[1], endpoints[1]))
    if scenario not in ("E0", "S0", "S1", "S2"):
        raise ValueError(f"Unknown scenario {scenario}")
    expected = cfg.get("scenarios", {}).get(scenario, {})
    if scenario in ("S1", "S2") and (not plans or set(primary) != set(plans)):
        raise ValueError("Missing physical events or preregistered receivers")
    if scenario in ("E0", "S0") and plans:
        raise ValueError(f"Unexpected brake registration in {scenario}")
    if len(plans) != expected.get("events", len(plans)):
        raise ValueError(f"{scenario}: physical-event denominator disagrees with snapshot")
    if plans and sum(map(len, primary.values())) != expected.get("target_pairs", sum(map(len, primary.values()))):
        raise ValueError(f"{scenario}: event-pair denominator disagrees with snapshot")
    if len({p.sender for p in plans.values()}) != len(plans):
        raise ValueError("Simultaneous physical events on the same sender are ambiguous")
    return plans, primary


def truth_and_physics(logs, plans, primary, cfg, clock, run_ids):
    timing, motion = cfg["timing"], cfg["motion"]
    duration = clock.tick(timing["duration_s"])
    step = clock.tick(timing["truth_step_s"])
    if step <= 0 or step != clock.tick(timing["motion_step_s"]):
        raise ValueError("Core motion and independent-truth grids must agree")
    physical_log = {}
    for row in read_rows(logs / "event_log.csv", "planned_event_id sender_id t0_lower_bound t0 event_status exclusion_reason", run_ids):
        name = row["planned_event_id"]
        if name in physical_log or name not in plans or row["sender_id"] != plans[name].sender:
            raise ValueError(f"Bad physical event key: {name}")
        if row["event_status"] != "occurred" or row["exclusion_reason"].strip():
            raise ValueError(f"Physical brake did not occur: {name} ({row['exclusion_reason']})")
        physical_log[name] = (clock.tick(row["t0_lower_bound"]), clock.tick(row["t0"]))
    if set(physical_log) != set(plans):
        raise ValueError("Missing physical-event records")

    # Retain speeds only for the few preregistered physical senders. Receivers need grid coverage only.
    speeds = {p.sender: [] for p in plans.values()}
    targets = set(speeds) | {v for receivers in primary.values() for _, v in receivers}
    extent = {}
    for row in read_rows(logs / "scenario_truth.csv", "t vehicle_id speed_mps", run_ids):
        vehicle = row["vehicle_id"]
        if vehicle not in targets:
            continue
        stamp = clock.tick(row["t"])
        previous = extent.get(vehicle)
        if previous and stamp - previous[1] != step:
            raise ValueError(f"{vehicle}: missing, duplicate or unordered motion tick")
        if stamp % step:
            raise ValueError(f"{vehicle}: independent truth off global motion grid")
        extent[vehicle] = (previous[0] if previous else stamp, stamp)
        speed = finite(row["speed_mps"])
        if speed < 0:
            raise ValueError(f"{vehicle}: negative speed")
        if vehicle in speeds:
            speeds[vehicle].append((stamp, speed))
    actual = {}
    for name, event in plans.items():
        l_ref, u_ref = physical_log[name]
        first_required = event.command - clock.tick(1)
        end_required = u_ref + max(event.window, clock.tick(timing["post_event_s"]))
        for vehicle in (event.sender, *(v for _, v in primary[name])):
            if (vehicle not in extent or extent[vehicle][0] > first_required or
                    extent[vehicle][1] + step < end_required or end_required > duration):
                raise ValueError(f"{name}: {vehicle} absent or observation too short")
        track = speeds[event.sender]
        if track[-1][0] + step < duration:
            raise ValueError(f"{name}: stop-hold not observed to run end")
        cruise = finite(motion["speed_kmh"]) / 3.6
        if any(abs(v - cruise) > 0.5 for t, v in track if first_required <= t <= event.command):
            raise ValueError(f"{name}: incorrect pre-event cruise speed")
        accel = [(t, (v - last_v) / clock.seconds(step), last_v - v)
                 for (_, last_v), (t, v) in zip(track, track[1:]) if t > event.command]
        onset = next((t for t, _, drop in accel if drop > 1e-6), None)
        if onset is None or (onset - step, onset) != (l_ref, u_ref):
            raise ValueError(f"{name}: independent first speed drop disagrees with physical log")
        stop = next((t for t, v in track if t >= onset and v <= finite(motion["stop_tolerance_mps"])), None)
        if (stop is None or stop > event.command + event.brake_duration + clock.tick("0.3") or
                track[-1][0] < stop + 2 * step or
                any(v > finite(motion["stop_tolerance_mps"]) for t, v in track if t >= stop)):
            raise ValueError(f"{name}: stop missing, late, or not maintained")
        decel = [a for t, a, _ in accel if onset <= t <= stop]
        streak = longest = 0
        for value in decel:
            streak = streak + 1 if value <= -finite(motion["brake_threshold_mps2"]) else 0
            longest = max(longest, streak)
        if len(decel) < 3 or longest < 3 or abs(fmean(decel[1:-1]) + finite(motion["deceleration_mps2"])) > 0.5:
            raise ValueError(f"{name}: independent braking strength fails")
        actual[name] = {"lower": l_ref, "upper": u_ref, "stop": stop,
                        "mean_mid_deceleration_mps2": fmean(decel[1:-1]), "threshold_streak_ticks": longest}
    return actual


def detections(logs, plans, actual, timing, clock, run_ids):
    by_sender = {plan.sender: name for name, plan in plans.items()}
    found = {}
    for row in read_rows(logs / "detection_log.csv", "sender_id event_id t_detect event_valid_until", run_ids):
        name = by_sender.get(row["sender_id"])
        if name is None:
            if row.get("planned_event_id"):
                raise ValueError("Detection explicitly maps to unrelated sender")
            continue
        stamp = clock.tick(row["t_detect"])
        if stamp < actual[name]["upper"]:
            if row.get("planned_event_id"):
                raise ValueError("Early detection explicitly matched to physical event")
            continue
        until = clock.tick(row["event_valid_until"])
        event_id = int(row["event_id"])
        if (row.get("planned_event_id") not in (None, "", name) or event_id < 0 or name in found
                or until != stamp + clock.tick(timing["event_validity_s"]) or stamp > clock.tick(timing["duration_s"])):
            raise ValueError(f"{name}: ambiguous or malformed detection key/timing")
        found[name] = (event_id, stamp, until)
    return found


def note_first(values, key, stamp, seq):
    if key not in values or (stamp, seq) < values[key]:
        values[key] = (stamp, seq)


def project_receptions(logs, plans, primary, actual, detections_by_name, clock, run_ids):
    endpoints = {(plans[name].sender, receiver): (name, pair)
                 for name, rows in primary.items() for pair, receiver in rows}
    first = defaultdict(dict)
    cursor = {}
    columns = "receiver_id sender_id seq t_rx t_gen state_valid state_accepted event_valid event_id event_detect_time event_valid_until"
    for row in read_rows(logs / "rx_log.csv", columns, run_ids):
        channel = row["sender_id"], row["receiver_id"]
        if channel not in endpoints:
            continue
        name, pair = endpoints[channel]
        r, g, seq = clock.tick(row["t_rx"]), clock.tick(row["t_gen"]), int(row["seq"])
        if seq < 0 or min(r, g) < 0:
            raise ValueError(f"{name}: negative packet sequence/time")
        valid, accepted, carries = (flag(row[k]) for k in ("state_valid", "state_accepted", "event_valid"))
        last_rx, last_seq, last_gen = cursor.get(channel, (-1, -1, -1))
        if r < last_rx:
            raise ValueError(f"{name}: nonchronological receive log for {channel}")
        fresh = valid and g <= r and seq > last_seq and g >= last_gen
        if accepted and not fresh:
            raise ValueError(f"{name}: impossible accepted duplicate, future or stale state")
        cursor[channel] = (r, seq if accepted else last_seq, g if accepted else last_gen)
        detection = detections_by_name.get(name)
        informative = False
        if carries and fresh and detection:
            ident, detect, until = detection
            informative = (int(row["event_id"]) == ident and clock.tick(row["event_detect_time"]) == detect
                           and clock.tick(row["event_valid_until"]) == until and detect <= g <= r <= until)
        for which in ("lower", "upper"):
            start = actual[name][which]
            if start <= r <= start + plans[name].window:
                values = first[name, pair, which]
                if accepted and g >= start:
                    note_first(values, "update", r, seq)
                if informative:
                    note_first(values, "info", r, seq)
    return first


def outcome(first, onset, event, clock):
    info, update = first.get("info"), first.get("update")
    waiting = info[0] - onset if info else event.window
    return {"D_info_s": clock.seconds(waiting) if info else None,
            "D_update_s": clock.seconds(update[0] - onset) if update else None,
            "first_event_rx_s": clock.seconds(info[0]) if info else None,
            "first_update_rx_s": clock.seconds(update[0]) if update else None,
            "first_event_seq": info[1] if info else None, "first_update_seq": update[1] if update else None,
            "delivered": bool(info), "no_receive": not info, "right_censored": not info,
            "deadline_miss": not info or waiting > event.deadline,
            "restricted_wait_s": clock.seconds(waiting)}


def analyze_events(attempt: Path, cfg: dict) -> dict:
    """Validate independently measured brakes, then project them onto registered receivers."""
    inputs, logs = Path(attempt) / "inputs", Path(attempt) / "logs"
    clock = Clock(cfg["timing"]["time_resolution_s"])
    run_ids = set()
    plans, primary = registrations(inputs, cfg, clock, run_ids)
    actual = truth_and_physics(logs, plans, primary, cfg, clock, run_ids)
    detected = detections(logs, plans, actual, cfg["timing"], clock, run_ids)
    first = project_receptions(logs, plans, primary, actual, detected, clock, run_ids)
    events, physical = [], []
    for name, plan in plans.items():
        motion = actual[name]
        lower, upper = motion["lower"], motion["upper"]
        detection = detected.get(name)
        fact = {"planned_event_id": name, "sender_id": plan.sender,
                "planned_time_s": clock.seconds(plan.command), "physical_occurred": True,
                "t0_lower_bound_s": clock.seconds(lower), "t0_s": clock.seconds(upper),
                "stop_time_s": clock.seconds(motion["stop"]), "threshold_streak_ticks": motion["threshold_streak_ticks"],
                "mean_mid_deceleration_mps2": motion["mean_mid_deceleration_mps2"],
                "detected": bool(detection), "event_id": detection[0] if detection else None,
                "t_detect_s": clock.seconds(detection[1]) if detection else None}
        physical.append(fact)
        for pair, receiver in primary[name]:
            at_u = outcome(first[name, pair, "upper"], upper, plan, clock)
            at_l = outcome(first[name, pair, "lower"], lower, plan, clock)
            sensitive = any(at_u[k] != at_l[k] for k in ("first_event_seq", "first_update_seq", "deadline_miss"))
            upper_info = first[name, pair, "upper"].get("info")
            fixed_bounds = ([clock.seconds(upper_info[0] - upper), clock.seconds(upper_info[0] - lower)]
                            if upper_info else None)
            events.append({**fact, "pair_id": pair, "receiver_id": receiver,
                           "deadline_s": clock.seconds(plan.deadline), "observation_s": clock.seconds(plan.window),
                           **at_u, "at_lower_endpoint": at_l, "classification_or_selection_sensitive": sensitive,
                           "physical_classification": "unresolved" if at_l["deadline_miss"] != at_u["deadline_miss"]
                           or at_l["delivered"] != at_u["delivered"] else "same_at_both_endpoints",
                           "fixed_info_delay_interval_s_closed_open": fixed_bounds})
    n = len(events)
    success = [row["D_info_s"] for row in events if row["delivered"]]
    first_t0 = min((row["t0_s"] for row in physical), default=None)
    last_stop = max((row["stop_time_s"] for row in physical), default=None)
    run = {"physical_event_n": len(physical), "event_pair_n": n, "delivered_n": len(success),
           "conditionally_successful_n": len(success), "deadline_miss_n": sum(row["deadline_miss"] for row in events),
           "no_receive_n": n - len(success), "not_detected_n": sum(not row["detected"] for row in events),
           "physical_not_detected_n": len(physical) - len(detected),
           "restricted_mean_wait_s": fmean(row["restricted_wait_s"] for row in events) if n else None,
           "conditional_mean_D_info_s": fmean(success) if success else None,
           "earliest_t0_s": first_t0, "last_stop_s": last_stop,
           "na_reason": None if n else "no_preregistered_braking_task"}
    for key in ("deadline_miss", "no_receive", "not_detected"):
        run[key + "_fraction"] = run[key + "_n"] / n if n else None
    return {"events": events, "physical_events": physical, "run": run,
            "earliest_t0_s": first_t0, "last_stop_s": last_stop}
