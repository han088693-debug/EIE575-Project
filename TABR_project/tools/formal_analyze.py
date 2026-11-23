"""Validate one completed or currently finalizing EIE575 attempt, then compute metrics."""

import csv
from collections import Counter
from contextlib import closing
from decimal import Decimal, InvalidOperation
import heapq
import json
from pathlib import Path
import sqlite3
import tempfile


HEADERS = {
    k: v.split()
    for k, v in {
        "events": "planned_event_id sender_id planned_time_s target_speed_mps brake_duration_s deadline_s observation_s",
        "pairs": "pair_id sender_id receiver_id role planned_event_id eval_start eval_end",
        "prior": "sender_id receiver_id init_time prior_gen_time position_m speed_mps source",
        "lifecycle": "run_id vehicle_id enter_s exit_s tx_count rx_count busy_audited",
        "event_log": "run_id planned_event_id sender_id t0_lower_bound t0 event_status exclusion_reason",
        "detection_log": "run_id sender_id event_id t_detect event_valid_until",
        "scenario_truth": "run_id t vehicle_id position_m speed_mps",
        "state_log": "run_id t receiver_id sender_id known state_source last_seq last_state_gen_time p_last_m v_last_mps p_hat_m Age_s",
        "control_log": "run_id vehicle_id tick_time window_start window_end busy_duration_s C Cbar e_H B S U W b r speed_mps",
        "tx_log": "run_id sender_id seq t_gen t_submit position_m speed_mps brake_active event_id event_detect_time event_valid_until message_bits",
        "rx_log": "run_id receiver_id sender_id seq t_rx t_gen position_m speed_mps brake_active event_id event_detect_time event_valid_until state_valid state_accepted event_valid invalid_reason",
        "busy_audit": "run_id vehicle_id transition_time busy_state",
        "local_supply": "run_id t vehicle_id background_count",
    }.items()
}
INPUTS = {"events", "pairs", "prior"}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def number(value):
    try:
        x = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"Invalid decimal {value!r}") from error
    require(x.is_finite(), f"Nonfinite decimal {value!r}")
    return x


def integer(value):
    require(str(value).isascii() and str(value).isdigit(), f"Invalid unsigned integer {value!r}")
    return int(value)


def flag(value):
    require(value in ("0", "1"), f"Invalid boolean {value!r}")
    return value == "1"


def read_table(root, table, rid):
    path = root / ("inputs" if table in INPUTS else "logs") / (table + ".csv")
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, strict=True)
        cols = reader.fieldnames or []
        require(
            len(set(cols)) == len(cols) and set(HEADERS[table]) <= set(cols),
            f"{path.name}: missing or repeated columns {set(HEADERS[table])-set(cols)}",
        )
        for row in reader:
            require(
                None not in row and None not in row.values(),
                f"{path.name}:{reader.line_num}: truncated or extra fields",
            )
            if "run_id" in row:
                require(row["run_id"] == rid, f"{path.name}:{reader.line_num}: run_id mismatch")
            yield row


def checked_time(rows, key):
    previous = Decimal("-Infinity")
    for row in rows:
        t = number(row[key])
        require(t >= previous, f"{key}: time order violation")
        previous = t
        yield t, row


def packet_payload(r):
    event = r["event_id"]
    brake = flag(r["brake_active"])
    gen, pos, speed = (number(r[k]) for k in ("t_gen", "position_m", "speed_mps"))
    require(speed >= 0, "Negative packet speed")
    if event in ("", "-1"):
        require(
            all(
                r[k] in ("", "-1") or number(r[k]) == -1
                for k in ("event_detect_time", "event_valid_until")
            ),
            "Unexpected empty-event payload",
        )
        return gen, pos, speed, brake, None, None, None
    detect, expiry = number(r["event_detect_time"]), number(r["event_valid_until"])
    require(0 <= detect <= gen <= expiry, "Inconsistent nonempty TX event payload")
    return gen, pos, speed, brake, event, detect, expiry


def payload_key(payload):
    return json.dumps([str(v.normalize()) if isinstance(v, Decimal) else v for v in payload])


def audit_transport(root, cfg, manifest):
    rid = manifest["run_id"]
    table = lambda name: read_table(root, name, rid)
    # Reconcile each producer's end-of-life counters, including legitimately empty streams.
    life = {}
    trimmed = manifest.get("observation_mode", "full") == "registered_pairs"
    require(
        manifest.get("observation_mode", "full") in ("full", "registered_pairs"),
        "Unknown observation_mode",
    )
    duration = number(cfg["timing"]["duration_s"])
    for r in table("lifecycle"):
        v = r["vehicle_id"]
        a, b = number(r["enter_s"]), number(r["exit_s"])
        tx, rx = integer(r["tx_count"]), integer(r["rx_count"])
        lx, lr = (integer(r[k]) for k in ("logged_tx_count", "logged_rx_count")) if trimmed else (tx, rx)
        require(
            v and v not in life and 0 <= a <= b <= duration and lx <= tx and lr <= rx,
            "Invalid lifecycle row",
        )
        life[v] = (a, b, tx, rx, lx, lr)
    require(bool(life), "Missing lifecycle observations")
    pairs, ids = {}, set()
    for p in table("pairs"):
        pair = p["sender_id"], p["receiver_id"]
        require(pair[0] != pair[1] and all(v in life for v in pair), "Missing registered vehicle")
        require(
            p["pair_id"] and p["pair_id"] not in ids and p["role"] in ("target", "background"),
            "Invalid registered pair",
        )
        ids.add(p["pair_id"])
        if pair in pairs:
            require(pairs[pair] == p["role"], "Conflicting roles for the same observation")
        pairs[pair] = p["role"]
    require(
        sum(v == "target" for v in pairs.values()) == cfg["scenarios"][cfg["scenario"]]["target_pairs"],
        "Target pair count mismatch",
    )
    require(
        sum(v == "background" for v in pairs.values()) == cfg["geometry"]["background_pair_count"],
        "Background pair count mismatch",
    )
    priors = {}
    warmup = number(cfg["timing"]["warmup_s"])
    for r in table("prior"):
        pair = r["sender_id"], r["receiver_id"]
        require(
            pair in pairs
            and pairs[pair] == "target"
            and pair not in priors
            and r["source"] == "given_prior",
            "Unregistered or duplicate prior",
        )
        init, gen, pos, speed = (
            number(r[k]) for k in ("init_time", "prior_gen_time", "position_m", "speed_mps")
        )
        require(gen == init == warmup and speed == 0, "Invalid stationary prior")
        priors[pair] = ("given_prior", None, gen, pos, speed)
    prior_pairs = {k for k, v in pairs.items() if v == "target"} if cfg["scenario"] == "S0" else set()
    require(prior_pairs == set(priors), "Missing or unexpected stationary prior")
    counts_tx, counts_rx = Counter(), Counter()
    states = {}
    injected = set()
    eps = number(cfg["timing"]["time_resolution_s"])

    def inside(vehicle, t):
        require(
            vehicle in life and life[vehicle][0] - eps <= t <= life[vehicle][1] + eps,
            f"Vehicle outside lifecycle: {vehicle} at {t}",
        )

    bits = cfg["wireless"]["message_bits"]
    with tempfile.TemporaryDirectory(prefix="tabr_packets_") as tmp, closing(
        sqlite3.connect(str(Path(tmp) / "index.db"))
    ) as db:
        db.execute(
            "CREATE TABLE tx(sender TEXT, seq INTEGER, payload TEXT, submit TEXT, PRIMARY KEY(sender,seq)) WITHOUT ROWID"
        )
        for t, r in checked_time(table("tx_log"), "t_submit"):
            s, seq = r["sender_id"], integer(r["seq"])
            inside(s, t)
            payload = packet_payload(r)
            require(
                0 <= payload[0] <= t and integer(r["message_bits"]) == bits,
                "Invalid TX time or message bits",
            )
            try:
                db.execute("INSERT INTO tx VALUES(?,?,?,?)", (s, seq, payload_key(payload), str(t)))
            except sqlite3.IntegrityError as e:
                raise ValueError(f"Duplicate TX packet {s}/{seq}") from e
            counts_tx[s] += 1

        def tagged(name, field, priority):
            for t, row in checked_time(table(name), field):
                yield t, priority, row

        streams = (tagged("rx_log", "t_rx", 0), tagged("state_log", "t", 1))
        for t, priority, r in heapq.merge(*streams, key=lambda item: item[:2]):
            s, v = pair = (r["sender_id"], r["receiver_id"])
            inside(v, t)
            if pair in priors and pair not in injected and t >= warmup:
                states[pair] = priors[pair]
                injected.add(pair)
            if priority == 1:
                require(pair in pairs, "Unregistered state sample")
                state = states.get(pair)
                require(
                    flag(r["known"]) == (state is not None),
                    "State known flag differs from prior/RX replay",
                )
                if state is None:
                    require(
                        r["state_source"] == "unknown"
                        and all(
                            r[k] == ""
                            for k in (
                                "last_seq",
                                "last_state_gen_time",
                                "p_last_m",
                                "v_last_mps",
                                "p_hat_m",
                                "Age_s",
                            )
                        ),
                        "Nonempty unknown state",
                    )
                else:
                    src, seq, gen, pos, speed = state
                    require(
                        r["state_source"] == src
                        and (r["last_seq"] == "" if seq is None else integer(r["last_seq"]) == seq),
                        "State source/sequence differs from prior/RX replay",
                    )
                    require(
                        abs(number(r["last_state_gen_time"]) - gen) <= eps
                        and abs(number(r["Age_s"]) - (t - gen)) <= eps,
                        "Invalid state age or generation time",
                    )
                    require(
                        abs(number(r["p_last_m"]) - pos) <= Decimal("1e-7")
                        and abs(number(r["v_last_mps"]) - speed) <= Decimal("1e-7"),
                        "State payload differs from prior/RX replay",
                    )
                continue
            counts_rx[v] += 1
            seq = integer(r["seq"])
            payload = packet_payload(r)
            gen, pos, speed, _, event, detect, expiry = payload
            sent = db.execute(
                "SELECT payload,submit FROM tx WHERE sender=? AND seq=?", (s, seq)
            ).fetchone()
            require(
                sent is not None and sent[0] == payload_key(payload) and number(sent[1]) <= t,
                f"RX with unmatched TX/payload or negative delivery time: {s}/{seq}",
            )
            valid_state, accepted, event_valid = (
                flag(r[k]) for k in ("state_valid", "state_accepted", "event_valid")
            )
            require(not accepted or valid_state, "Invalid state accepted")
            require(
                event_valid == (valid_state and event is not None and t <= expiry),
                "RX event validity disagrees with payload or expiration",
            )
            require(event_valid or bool(r["invalid_reason"]), "Missing RX rejection/no-event reason")
            if pair not in pairs:
                continue
            last = states.get(pair)
            fresh = (
                s != v
                and gen <= t
                and (last is None or (gen >= last[2] and (last[1] is None or seq > last[1])))
            )
            require(
                accepted == fresh,
                f"State acceptance disagrees with prior/duplicate/order replay: {s}->{v}/{seq}",
            )
            if accepted:
                states[pair] = ("received", seq, gen, pos, speed)
        for v, (_, __, tx, rx, lx, lr) in life.items():
            require(
                counts_tx[v] == lx and counts_rx[v] == lr,
                f"{v}: logged counts do not reconcile with lifecycle",
            )
    return {
        "vehicles": len(life),
        "registered_pairs": len(pairs),
        "logged_tx": sum(counts_tx.values()),
        "logged_rx": sum(counts_rx.values()),
        "all_tx": sum(x[2] for x in life.values()),
        "all_rx": sum(x[3] for x in life.values()),
        "given_priors": len(priors),
        "observation_mode": "registered_pairs" if trimmed else "full",
    }


def analyze_attempt(attempt):
    from formal_events import analyze_events
    from formal_state import analyze_state

    root = Path(attempt)
    try:
        cfg = json.loads((root / "config_snapshot.json").read_text(encoding="utf-8-sig"))
        manifest = json.loads((root / "run_manifest.json").read_text(encoding="utf-8-sig"))
        for key in ("scenario", "scheme", "seed", "load_id", "data_origin"):
            if key in cfg and key in manifest:
                require(cfg[key] == manifest[key], f"Snapshot/manifest disagree on {key}")
        cfg = {
            **cfg,
            **{
                k: manifest[k]
                for k in ("scenario", "scheme", "seed", "load_id", "data_origin")
                if k in manifest
            },
        }
        require(
            bool(manifest["run_id"]) and cfg["scenario"] in cfg["scenarios"],
            "Missing run/scenario identity",
        )
        origin = cfg.get("data_origin") or (
            "synthetic" if manifest.get("scope") == "synthetic" else "simulation"
        )
        require(origin in ("synthetic", "simulation"), "Invalid data_origin")
        require(
            manifest.get("scope") != "formal" or origin == "simulation",
            "Synthetic input in formal attempt",
        )
        require(
            manifest.get("scope") != "synthetic" or origin == "synthetic",
            "Synthetic run mislabeled as simulation",
        )
        # Ensure every non-transport row (including rows outside metric windows)
        # belongs to the run and has the declared field count. Correct empty tables retain headers.
        for name in (
            "events",
            "event_log",
            "detection_log",
            "scenario_truth",
            "control_log",
            "busy_audit",
        ):
            for _ in read_table(root, name, manifest["run_id"]):
                pass
        if cfg["scenario"] == "E0":
            rows = read_table(root, "local_supply", manifest["run_id"])
            require(
                sum(1 for row in rows if integer(row["background_count"]) >= 0) > 0,
                "Missing E0 local supply",
            )
            diag = json.loads((root / "logs" / "sumo_diagnostics.json").read_text(encoding="utf-8-sig"))
            names = (
                "expected_entries",
                "actual_entries",
                "insertion_failures",
                "unexplained_teleports",
                "unexplained_removals",
            )
            require(
                isinstance(diag, dict) and all(type(diag.get(k)) is int and diag[k] >= 0 for k in names),
                "Invalid SUMO diagnostics",
            )
            require(
                diag["actual_entries"] == diag["expected_entries"]
                and not any(diag[k] for k in names[2:]),
                "Unreconciled SUMO diagnostics",
            )
        audit = audit_transport(root, cfg, manifest)
        event = analyze_events(root, cfg)
        state = analyze_state(root, cfg, event)
        return {
            "schema_version": cfg["schema_version"],
            "analysis_version": cfg["analysis"]["method_version"],
            "run_id": manifest["run_id"],
            "data_validity": "valid",
            "data_origin": origin,
            "scenario": cfg["scenario"],
            "scheme": cfg["scheme"],
            "seed": cfg["seed"],
            "load_id": cfg["load_id"],
            "transport": audit,
            **event,
            "state": state,
        }
    except (OSError, KeyError, TypeError, ArithmeticError, csv.Error, sqlite3.Error) as error:
        raise ValueError(f"Incomplete or invalid attempt {root.name}: {error}") from error
