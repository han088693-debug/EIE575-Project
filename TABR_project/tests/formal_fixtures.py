"""Synthetic raw simulator for exercising the same attempt schema and batch lifecycle."""

import argparse
from collections import Counter
from contextlib import ExitStack
import csv
from decimal import Decimal
import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "tools"))
from formal_analyze import HEADERS, number


def fixture_config(scenario="S1"):
    cfg = json.loads((PROJECT / "configs/core.json").read_text(encoding="utf-8"))
    cfg.update(scenario=scenario, scheme="G0", seed=200, load_id="low", data_origin="synthetic")
    cfg["timing"].update(duration_s=17, warmup_s=6, stationary_eval_s=10)
    cfg["geometry"]["background_pair_count"] = 1
    return cfg


def pairs_for(cfg):
    scenario = cfg["scenario"]
    pairs = [
        (f"p{i}", f"lead{i}", f"follow{i}", "target")
        for i in range(cfg["scenarios"][scenario]["target_pairs"])
    ]
    return pairs + [
        (f"b{i}", f"bg_lead{i}", f"bg_follow{i}", "background")
        for i in range(cfg["geometry"]["background_pair_count"])
    ]


def write_rows(path, table, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADERS[table])
        w.writeheader()
        w.writerows(rows)


def prepare_fixture_inputs(config, job, inputs_dir):
    cfg = {**config, **{k: job[k] for k in ("scenario", "seed", "scheme", "load_id") if k in job}}
    dest = Path(inputs_dir)
    dest.mkdir(parents=True, exist_ok=True)
    warmup = number(cfg["timing"]["warmup_s"])
    duration = number(cfg["timing"]["duration_s"])
    events = []
    pairs = []
    priors = []
    speed = number(cfg["motion"]["speed_kmh"]) / Decimal("3.6")
    for pair, s, v, role in pairs_for(cfg):
        brake = cfg["scenario"] in ("S1", "S2") and role == "target"
        key = "brake_" + s if brake else ""
        pairs.append(
            dict(
                pair_id=pair,
                sender_id=s,
                receiver_id=v,
                role=role,
                planned_event_id=key,
                eval_start=0,
                eval_end=duration,
            )
        )
        if brake:
            events.append(
                dict(
                    planned_event_id=key,
                    sender_id=s,
                    planned_time_s=warmup,
                    target_speed_mps=0,
                    brake_duration_s=speed / number(cfg["motion"]["deceleration_mps2"]),
                    deadline_s=cfg["timing"]["deadline_s"],
                    observation_s=cfg["timing"]["observation_s"],
                )
            )
        if cfg["scenario"] == "S0" and role == "target":
            priors.append(
                dict(
                    sender_id=s,
                    receiver_id=v,
                    init_time=warmup,
                    prior_gen_time=warmup,
                    position_m=3000,
                    speed_mps=0,
                    source="given_prior",
                )
            )
    for table, rows in (("pairs", pairs), ("events", events), ("prior", priors)):
        write_rows(dest / (table + ".csv"), table, rows)


def run_engine(config, job, attempt):
    root = Path(attempt)
    manifest = json.loads((root / "run_manifest.json").read_text(encoding="utf-8"))
    cfg = {**config, **{k: job[k] for k in ("scenario", "seed", "scheme", "load_id") if k in job}}
    if manifest.get("scope") != "synthetic" and cfg.get("data_origin") != "synthetic":
        raise ValueError("Fixture engine requires a synthetic attempt")
    rid = manifest["run_id"]
    case = job.get("fixture_case", cfg.get("fixture_case", "on_time"))
    allowed = {
        "on_time",
        "late",
        "no_receive",
        "not_detected",
        "prior_only",
        "prior_replay",
        "prior_first_seq0",
        "wrong_bits",
        "missing_log",
        "truncated",
        "short_lived",
    }
    if case not in allowed:
        raise ValueError("Unknown fixture case: " + case)
    sc = cfg["scenario"]
    o = cfg["timing"]
    step = number(o["truth_step_s"])
    control = number(o["control_step_s"])
    warm = number(o["warmup_s"])
    dur = number(o["duration_s"])
    t0 = warm + number(o["motion_step_s"])
    accel = number(cfg["motion"]["deceleration_mps2"])
    v0 = number(cfg["motion"]["speed_kmh"]) / Decimal("3.6")
    valid = number(o["event_validity_s"])
    pairs = pairs_for(cfg)
    vehicles = {p for _, s, v, _ in pairs for p in (s, v)}
    fixed = {s for _, s, _, role in pairs if role == "target"}
    fixed.update(v for _, _, v, role in pairs if role == "target")
    braking = fixed if sc in ("S1", "S2") else set()
    offset = {s: Decimal(3000 + 100 * i) for i, (_, s, v, role) in enumerate(pairs)}
    offset.update({v: offset[s] - 30 for _, s, v, role in pairs})

    def motion(v, t):
        if sc == "S0" and v in fixed:
            return offset[v], Decimal(0)
        dt = min(max(t - warm, Decimal(0)), v0 / accel) if v in braking else Decimal(0)
        pos = offset[v] + v0 * t if not dt else offset[v] + v0 * warm + v0 * dt - accel * dt * dt / 2
        return pos, max(Decimal(0), v0 - accel * dt)

    received = []
    sent = []
    detections = []
    events = []

    def packet(s, v, seq, gen, submit, arrival, event, accepted=True):
        pos, speed = motion(s, gen)
        payload = dict(
            sender_id=s,
            seq=seq,
            t_gen=gen,
            position_m=pos,
            speed_mps=speed,
            brake_active=int(event),
            event_id="0" if event else "-1",
            event_detect_time=t0 if event else "-1",
            event_valid_until=t0 + valid if event else "-1",
        )
        sent.append(
            dict(
                **payload,
                t_submit=submit,
                message_bits=cfg["wireless"]["message_bits"] + (case == "wrong_bits"),
            )
        )
        if arrival is not None:
            received.append(
                dict(
                    **payload,
                    receiver_id=v,
                    t_rx=arrival,
                    state_valid=int(accepted),
                    state_accepted=int(accepted),
                    event_valid=int(event and accepted and arrival <= t0 + valid),
                    invalid_reason=(
                        ""
                        if event and accepted
                        else ("no_event" if accepted else "duplicate_or_out_of_order_state")
                    ),
                )
            )

    for pair, s, v, role in pairs:
        if role != "target":
            continue
        if sc in ("S1", "S2"):
            events.append(
                dict(
                    planned_event_id="brake_" + s,
                    sender_id=s,
                    t0_lower_bound=warm,
                    t0=t0,
                    event_status="occurred",
                    exclusion_reason="",
                )
            )
            if case != "not_detected":
                detections.append(
                    dict(sender_id=s, event_id=0, t_detect=t0, event_valid_until=t0 + valid)
                )
            packet(
                s,
                v,
                0,
                t0,
                t0,
                (
                    None
                    if case == "no_receive"
                    else t0 + (Decimal("1") if case == "late" else Decimal("0.1"))
                ),
                case != "not_detected",
            )
        if sc == "S0" and case == "prior_replay":
            # Preheated state, old in-flight packet, fresh same-tick arrival, duplicate.
            packet(s, v, 0, warm - step * 2, warm - step * 2, warm - step, False)
            packet(s, v, 1, warm - step, warm - step, warm, False, False)
            packet(s, v, 2, warm, warm, warm, False)
            received.append(
                dict(
                    received[-1],
                    t_rx=warm + step,
                    state_valid=0,
                    state_accepted=0,
                    event_valid=0,
                    invalid_reason="duplicate_or_out_of_order_state",
                )
            )
        if sc == "S0" and case == "prior_first_seq0":
            packet(s, v, 0, warm, warm, warm, False)
    sent.sort(key=lambda r: r["t_submit"])
    received.sort(key=lambda r: r["t_rx"])
    tx = Counter(row["sender_id"] for row in sent)
    rx = Counter(row["receiver_id"] for row in received)
    root.joinpath("logs").mkdir(exist_ok=True)
    with ExitStack() as stack:
        writers = {}
        for table in HEADERS.keys() - {"events", "pairs", "prior"}:
            if table == "local_supply" and sc != "E0":
                continue
            f = stack.enter_context(
                (root / "logs" / (table + ".csv")).open("w", encoding="utf-8", newline="")
            )
            w = csv.DictWriter(f, fieldnames=HEADERS[table])
            w.writeheader()
            writers[table] = w

        def put(table, row):
            writers[table].writerow(dict(run_id=rid, **row))

        for table, rows in (
            ("event_log", events),
            ("detection_log", detections),
            ("tx_log", sent),
            ("rx_log", received),
        ):
            for row in rows:
                put(table, row)
        for v in sorted(vehicles):
            put(
                "lifecycle",
                dict(
                    vehicle_id=v, enter_s=0, exit_s=dur, tx_count=tx[v], rx_count=rx[v], busy_audited=1
                ),
            )
            put("busy_audit", dict(vehicle_id=v, transition_time=0, busy_state=0))
        if case == "short_lived":
            put(
                "lifecycle",
                dict(
                    vehicle_id="short",
                    enter_s=1,
                    exit_s=Decimal("1.01"),
                    tx_count=0,
                    rx_count=0,
                    busy_audited=0,
                ),
            )
        states = {}
        idx = 0
        t = Decimal(0)
        rate_base = number(cfg["control"]["b_initial_hz"])
        while t < dur:
            if t == warm and sc == "S0":
                for _, s, v, role in pairs:
                    if role == "target":
                        states[s, v] = ("given_prior", "", warm, *motion(s, warm))
            while idx < len(received) and received[idx]["t_rx"] <= t:
                r = received[idx]
                idx += 1
                if r["state_accepted"]:
                    states[r["sender_id"], r["receiver_id"]] = (
                        "received",
                        r["seq"],
                        r["t_gen"],
                        r["position_m"],
                        r["speed_mps"],
                    )
            is_control = t >= control and t % control == 0
            if is_control:
                rate_base = min(
                    number(cfg["control"]["b_max_hz"]),
                    rate_base
                    + number(cfg["control"]["feedback_gain"]) * number(cfg["control"]["cbr_target"]),
                )
            for v in sorted(vehicles):
                pos, speed = motion(v, t)
                put("scenario_truth", dict(t=t, vehicle_id=v, position_m=pos, speed_mps=speed))
                if is_control:
                    B = int(v in braking and warm < t <= warm + v0 / accel)
                    w = 1
                    if cfg["scheme"] == "G1":
                        w = min(
                            1 + number(cfg["control"]["beta"]),
                            max(
                                1, speed / (number(cfg["control"]["g1_reference_kmh"]) / Decimal("3.6"))
                            ),
                        )
                    if cfg["scheme"] == "G2":
                        w = 1 + number(cfg["control"]["beta"]) * B
                    H = number(cfg["control"]["lookback_s"])
                    prior_x, prior_v = motion(v, max(0, t - H))
                    err = abs(pos - prior_x - prior_v * H) if t >= H else 0
                    S = min(1, err / number(cfg["control"]["error_scale_m"]))
                    if cfg["scheme"] == "G3":
                        w = 1 + number(cfg["control"]["beta"]) * max(B, S)
                    put(
                        "control_log",
                        dict(
                            vehicle_id=v,
                            tick_time=t,
                            window_start=t - control,
                            window_end=t,
                            busy_duration_s=0,
                            C=0,
                            Cbar=0,
                            e_H=err,
                            B=B,
                            S=S,
                            U=max(B, S),
                            W=w,
                            b=rate_base,
                            r=min(rate_base * w, number(cfg["control"]["r_max_hz"])),
                            speed_mps=speed,
                        ),
                    )
                if sc == "E0" and v in fixed:
                    put(
                        "local_supply",
                        dict(t=t, vehicle_id=v, background_count=len(vehicles) - len(fixed)),
                    )
            for _, s, v, role in pairs:
                st = states.get((s, v))
                source, seq, gen, pos, speed = st if st else ("unknown", "", "", "", "")
                put(
                    "state_log",
                    dict(
                        t=t,
                        sender_id=s,
                        receiver_id=v,
                        known=int(st is not None),
                        state_source=source,
                        last_seq=seq,
                        last_state_gen_time=gen,
                        p_last_m=pos,
                        v_last_mps=speed,
                        p_hat_m=pos + speed * (t - gen) if st else "",
                        Age_s=t - gen if st else "",
                    ),
                )
            t += step
    if sc == "E0":
        entries = len(vehicles) + int(case == "short_lived")
        diagnostics = dict(
            expected_entries=entries,
            actual_entries=entries,
            collisions=0,
            insertion_failures=0,
            unexplained_teleports=0,
            unexplained_removals=0,
        )
        (root / "logs/sumo_diagnostics.json").write_text(
            json.dumps(diagnostics) + "\n", encoding="utf-8"
        )
    if case == "missing_log":
        (root / "logs/detection_log.csv").unlink()
    if case == "truncated":
        with (root / "logs/control_log.csv").open("a", encoding="utf-8") as f:
            f.write("broken")
    return {"exit_code": 0, "data_origin": "synthetic"}


def write_fixture(attempt, cfg=None, case="on_time"):
    root = Path(attempt)
    root.mkdir(parents=True, exist_ok=True)
    snap = root / "config_snapshot.json"
    manifest = root / "run_manifest.json"
    if cfg is None:
        cfg = json.loads(snap.read_text(encoding="utf-8")) if snap.exists() else fixture_config()
    cfg = {**cfg, "fixture_case": case}
    if not snap.exists():
        snap.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    if not manifest.exists():
        manifest.write_text(
            json.dumps(
                dict(
                    run_id="fixture",
                    attempt_id="fixture",
                    scope="synthetic",
                    status="completed",
                    **{k: cfg[k] for k in ("scenario", "scheme", "seed", "load_id", "data_origin")},
                )
            )
            + "\n",
            encoding="utf-8",
        )
    job = json.loads(manifest.read_text(encoding="utf-8"))
    if not (root / "inputs/events.csv").is_file():
        prepare_fixture_inputs(cfg, job, root / "inputs")
    run_engine(cfg, job, root)
    return root


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt", required=True, type=Path)
    parser.add_argument("--case", default="on_time")
    args = parser.parse_args()
    write_fixture(args.attempt, case=args.case)
