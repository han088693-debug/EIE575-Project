"""Formal state, motion and channel measurements. Dense network logs are read in time order."""
from bisect import bisect_right
from collections import Counter, defaultdict
import csv
from decimal import Decimal, ROUND_HALF_EVEN
import hashlib
from itertools import groupby
import json
import math
from pathlib import Path
import sqlite3
import tempfile
from terminal_coverage import check_final_control, completed_trips


def rows(path, required):
    with path.open(encoding="utf-8-sig", newline="") as fh:
        table = csv.DictReader(fh)
        missing = set(required.split()) - set(table.fieldnames or [])
        if missing:
            raise ValueError(f"{path.name}: missing {sorted(missing)}")
        for row in table:
            if None in row or any(v is None for v in row.values()):
                raise ValueError(f"{path.name}:{table.line_num}: malformed CSV row")
            yield row


def number(value):
    x = float(value)
    if not math.isfinite(x):
        raise ValueError(f"non-finite number: {value!r}")
    return x


def flag(value):
    if value not in ("0", "1"):
        raise ValueError(f"expected 0/1, got {value!r}")
    return value == "1"


class Clock:
    def __init__(self, timing):
        self.res = Decimal(str(timing["time_resolution_s"]))
        self.sec = float(self.res)

    def tick(self, value):
        d = Decimal(str(value))
        if not d.is_finite():
            raise ValueError(f"invalid time: {value!r}")
        return int((d / self.res).to_integral_value(rounding=ROUND_HALF_EVEN))


def overlap(a, b, c, d):
    return max(0, min(b, d) - max(a, c))


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def weighted_p95(pairs):
    entries = sorted(pairs)
    total = sum(w for _, w in entries)
    if not total:
        return None
    cumulative = 0
    for val, weight in entries:
        cumulative += weight
        if 20 * cumulative >= 19 * total:
            return val


def load_lifecycle(logs, tick, end):
    life = {}
    for row in rows(logs / "lifecycle.csv", "vehicle_id enter_s exit_s tx_count rx_count busy_audited"):
        ident = row["vehicle_id"]
        enter, leave = tick(row["enter_s"]), tick(row["exit_s"])
        tx, rx = int(row["tx_count"]), int(row["rx_count"])
        if not ident or ident in life or not 0 <= enter <= leave <= end or min(tx, rx) < 0:
            raise ValueError(f"invalid/duplicate vehicle lifecycle: {ident}")
        life[ident] = (enter, leave, tx, rx, flag(row["busy_audited"]))
    return life


def load_pairs(inputs, tick, life, analysis_window):
    result, ids = {}, set()
    start, end = analysis_window
    for row in rows(inputs / "pairs.csv", "pair_id sender_id receiver_id role eval_start eval_end"):
        pair_id, sender, receiver = row["pair_id"], row["sender_id"], row["receiver_id"]
        left, right = tick(row["eval_start"]), tick(row["eval_end"])
        if (not pair_id or pair_id in ids or (sender, receiver) in result or sender == receiver or
                row["role"] not in ("target", "background") or not left <= start < end <= right):
            raise ValueError(f"invalid registered pair/window: {pair_id}")
        for ident in (sender, receiver):
            if ident not in life or life[ident][0] > start or life[ident][1] < end:
                raise ValueError(f"pair {pair_id}: {ident} absent during evaluation")
        if row["role"] == "target" and not all(life[v][4] for v in (sender, receiver)):
            raise ValueError(f"pair {pair_id}: both target nodes require an independent busy audit")
        result[sender, receiver] = (pair_id, row["role"])
        ids.add(pair_id)
    if not any(role == "target" for _, role in result.values()):
        raise ValueError("no registered target pair")
    return result


def ceil_grid(t, step):
    return -(-t // step) * step


def load_truth(logs, clock, life, tracked, grid, start, end):
    data = {v: {} for v in tracked}
    prior_time = -1
    for row in rows(logs / "scenario_truth.csv", "t vehicle_id position_m speed_mps"):
        t, v = clock.tick(row["t"]), row["vehicle_id"]
        if v not in life or not life[v][0] <= t <= life[v][1] or t < prior_time:
            raise ValueError(f"out-of-order/out-of-lifetime truth: {v} tick {t}")
        prior_time = t
        if v in tracked:
            if t % grid or t in data[v]:
                raise ValueError(f"off-grid/duplicate independent truth: {v} tick {t}")
            data[v][t] = number(row["position_m"]), number(row["speed_mps"])
    for v, points in data.items():
        # A cropped file must not silently shorten H-history or change the motion hash.
        for t in range(ceil_grid(life[v][0], grid), life[v][1], grid):
            if t not in points:
                raise ValueError(f"missing independent truth: {v} tick {t}")
    return data


def stationary_truth(inputs, clock, targets, truth, window):
    start, end = window
    prior = {}
    for row in rows(inputs / "prior.csv", "sender_id receiver_id init_time prior_gen_time position_m speed_mps source"):
        p = row["sender_id"], row["receiver_id"]
        if (p in prior or p not in targets or row["source"] != "given_prior" or
                clock.tick(row["init_time"]) != start or clock.tick(row["prior_gen_time"]) != start or
                number(row["speed_mps"]) != 0):
            raise ValueError(f"invalid S0 stationary prior for {p}")
        prior[p] = number(row["position_m"])
    if set(prior) != set(targets):
        raise ValueError("S0 target/known prior keys do not match")
    for sender, receiver in targets:
        for v in (sender, receiver):
            if start not in truth[v]:
                raise ValueError(f"S0 prior initialization lacks independent truth: {v}")
            origin = truth[v][start][0]
            if v == sender and abs(origin - prior[sender, receiver]) > 1e-7:
                raise ValueError(f"S0 given prior position disagrees with truth: {v}")
            for t, (pos, speed) in truth[v].items():
                if start <= t < end and (abs(speed)>1e-8 or abs(pos-origin)>1e-7):
                    raise ValueError(f"S0 target moved during stationary evaluation: {v}")


def motion_mask(points, clock, step, lookback, scale, braking, tolerance):
    """Recompute innovation from independent truth, including interpolated H-history."""
    times = sorted(points)
    result = {}
    for i, t in enumerate(times):
        currx, currv = points[t]
        prev = times[i - 1] if i else t
        if t != prev and t - prev != step:
            raise ValueError(f"missing independent motion tick before {t}")
        brake = i > 0 and (currv - points[prev][1]) / ((t - prev) * clock.sec) <= -braking
        target = t - lookback
        ix = bisect_right(times, target) - 1
        score = 0.0
        if ix >= 0:
            a = times[ix]
            oldx, oldv = points[a]
            if target != a:
                b = times[ix + 1]
                frac = (target - a) / (b - a)
                oldx += (points[b][0] - oldx) * frac
                oldv += (points[b][1] - oldv) * frac
            score = min(1.0, abs(currx - oldx - oldv * (lookback * clock.sec)) / scale)
        result[t] = (not brake and score > tolerance, bool(brake), score)
    return result


class StateTotals:
    def __init__(self, thresholds):
        self.thresholds = thresholds
        self.total = self.known = 0
        self.error = self.age = 0.0
        self.over = [0] * len(thresholds)

    def add(self, weight, error, age):
        self.total += weight
        if error is not None:
            self.known += weight
            self.error += error * weight
            self.age += age * weight
            for i, threshold in enumerate(self.thresholds):
                if error > threshold:
                    self.over[i] += weight

    def result(self, sec):
        return {"exposure_s": self.total * sec, "known_s": self.known * sec,
                "unknown_fraction": (1 - self.known/self.total) if self.total else None,
                "mean_abs_error_m": self.error/self.known if self.known else None,
                "mean_age_s": self.age/self.known if self.known else None,
                "error_above_fraction_of_known": {f"{x:g}": w/self.known if self.known else None
                                                  for x, w in zip(self.thresholds, self.over)}}


def state_metrics(logs, clock, pairs, life, truth, masks, window, post, cfg):
    a, b = window
    grid = clock.tick(cfg["timing"]["truth_step_s"])
    thresholds = cfg["analysis"]["error_thresholds_m"]
    stats = {p: {k: StateTotals(thresholds) for k in ("full", "post", "active")} for p in pairs}
    last = {}
    prior_time = -1
    fields = "t sender_id receiver_id known state_source last_state_gen_time p_last_m v_last_mps p_hat_m Age_s"
    for row in rows(logs / "state_log.csv", fields):
        t = clock.tick(row["t"])
        p = row["sender_id"], row["receiver_id"]
        if (p not in pairs or t < prior_time or t % grid or t <= last.get(p, -1) or
                not all(life[v][0] <= t <= life[v][1] for v in p)):
            raise ValueError(f"unexpected/duplicate/off-grid state: {p}, tick {t}")
        prior_time = last[p] = t
        dt = overlap(t, t + grid, a, b)
        if not dt:
            continue
        source = row["state_source"]
        known = flag(row["known"])
        err = age = None
        if known:
            if source not in ("given_prior", "received") or (
                    source == "given_prior" and (cfg["scenario"] != "S0" or pairs[p][1] != "target")):
                raise ValueError(f"invalid state source for {p}: {source}")
            gen = clock.tick(row["last_state_gen_time"])
            age = (t - gen) * clock.sec
            estimate = number(row["p_last_m"]) + age * number(row["v_last_mps"])
            if (gen > t or abs(number(row["Age_s"]) - age) > clock.sec * 1.01 or
                    not math.isclose(number(row["p_hat_m"]), estimate, abs_tol=1e-8, rel_tol=1e-12)):
                raise ValueError(f"inconsistent state extrapolation: {p} at {t}")
            err = abs(truth[p[0]][t][0] - estimate)
        elif (source != "unknown" or any(row[col] for col in
              ("last_state_gen_time", "p_last_m", "v_last_mps", "p_hat_m", "Age_s")) or
                (cfg["scenario"] == "S0" and pairs[p][1] == "target")):
            raise ValueError(f"invalid unknown state/prior: {p} at {t}")
        stats[p]["full"].add(dt, err, age)
        if cfg["scenario"] in ("S1", "S2") and pairs[p][1] == "target":
            stats[p]["post"].add(overlap(t, t + grid, post, b), err, age)
            if masks[p[0]][t][0]:
                stats[p]["active"].add(dt, err, age)
    for p, entry in stats.items():
        if entry["full"].total != b - a:
            raise ValueError(f"incomplete state evaluation grid: {p}")
    return stats


class BusyAudit:
    """Consume each transition once, including transitions crossing control-window edges."""
    def __init__(self, path, life, tick):
        self.stream = iter(rows(path, "vehicle_id transition_time busy_state"))
        self.life, self.tick = life, tick
        self.state, self.used = {}, {}
        self.time = -1
        self.row = next(self.stream, None)

    def advance(self, end=None):
        while self.row is not None:
            row = self.row
            t = self.tick(row["transition_time"])
            if end is not None and t > end:
                break
            vehicle, busy = row["vehicle_id"], flag(row["busy_state"])
            if (vehicle not in self.life or not self.life[vehicle][4] or t < self.time or
                    not self.life[vehicle][0] <= t <= self.life[vehicle][1]):
                raise ValueError(f"invalid busy transition {vehicle} at {t}")
            self.time = t
            if vehicle not in self.state:
                if busy or t != self.life[vehicle][0]:
                    raise ValueError(f"missing initial idle state for {vehicle}")
                cumulative = 0
            else:
                previous, active, cumulative = self.state[vehicle]
                cumulative += (t - previous) * active
            self.state[vehicle] = t, busy, cumulative
            self.row = next(self.stream, None)

    def integral(self, vehicle, endpoint):
        if vehicle not in self.state:
            raise ValueError(f"missing busy audit for {vehicle}")
        t, busy, total = self.state[vehicle]
        cumulative = total + (endpoint - t) * busy
        delta = cumulative - self.used.get(vehicle, 0)
        self.used[vehicle] = cumulative
        return delta


def audit_controller(row, scheme, ctl, filtered, previous_base, settle_s):
    """Check the implementation's registered feedback/weight equations, per update."""
    def clip(x, lo, hi):
        return min(hi, max(lo, x))

    err, score, urgency, speed, base, weight, rate = [number(row[k]) for k in
            ("e_H", "S", "U", "speed_mps", "b", "W", "r")]
    brake = flag(row["B"])
    if err < 0 or speed < 0:
        raise ValueError("negative innovation or speed in controller")
    beta = ctl["beta"]
    expected_score = clip(err/ctl["error_scale_m"], 0, 1)
    if scheme == "G0":
        w = 1
    elif scheme == "G1":
        w = clip(speed / (ctl["g1_reference_kmh"]/3.6), 1, 1+beta)
    elif scheme == "G2":
        w = 1+beta*brake
    elif scheme == "G3":
        w = 1+beta*max(brake,expected_score)
    else:
        raise ValueError(f"unsupported controller scheme: {scheme}")
    # Match updateBase's roundoff allowance at the exact deadband edge.
    feedback_error = ctl["cbr_target"]-filtered
    b = previous_base
    # Mirror the controller's settle gate: no integration before the estimate is usable.
    if number(row["tick_time"]) + 1e-12 >= settle_s and abs(feedback_error) > ctl["deadband"]+1e-12:
        b = clip(b+ctl["feedback_gain"]*feedback_error, ctl["b_min_hz"], ctl["b_max_hz"])
    expected = {"S":(score,expected_score), "U":(urgency,(w-1)/beta if beta else 0),
                "b":(base,b), "W":(weight,w), "r":(rate,clip(b*w,ctl["r_min_hz"],ctl["r_max_hz"]))}
    for field,(actual,wanted) in expected.items():
        if not math.isclose(actual, wanted, rel_tol=1e-10, abs_tol=1e-9):
            raise ValueError(f"controller {field} fails independent calculation: {actual} vs {wanted}")
    return b


ESTIMATE_SETTLE_S = 1.0

def aggregate_controls(logs, clock, cfg, life, tracked, window, step, smoothing):
    a, b = window
    controls = {v: [] for v in tracked}
    previous, prev_cbar, previous_b = {}, {}, {}
    cumulative = defaultdict(lambda: [0, 0., 0., 0, 0])
    audit = BusyAudit(logs / "busy_audit.csv", life, clock.tick)
    max_error = 0.0
    count = 0
    c = cfg["control"]
    tol = cfg["analysis"].get("busy_integral_tolerance_s", 1e-8)
    max_rate, min_rate = c["r_max_hz"], c["r_min_hz"]
    fixed_rate = float(c.get("background_rate_hz", 0) or 0)
    target_ids = set()
    if fixed_rate > 0:
        pairs = Path(logs).parent / "inputs" / "pairs.csv"
        if pairs.is_file():
            for pair in rows(pairs, "sender_id receiver_id role"):
                if pair["role"] == "target":
                    target_ids.update((pair["sender_id"], pair["receiver_id"]))
        if not target_ids:
            raise ValueError("fixed background rate requires a registered target pair")
    fields = "vehicle_id tick_time window_start window_end busy_duration_s C Cbar r W"
    real = cfg.get("data_origin", "simulation") == "simulation"
    if real:
        fields += " e_H B S U b speed_mps"
    formula_count = 0
    # Exact weighted quantiles using a disk spool: memory does not grow with all network ticks.
    with tempfile.TemporaryDirectory(prefix="tabr-cbr-") as folder:
        conn = sqlite3.connect(str(Path(folder) / "eval.sqlite"))
        try:
            conn.execute("create table samples(vehicle text, c real, exposure integer)")
            pending = []
            last_t = -1
            for row in rows(logs / "control_log.csv", fields):
                v, t = row["vehicle_id"], clock.tick(row["tick_time"])
                left, right = clock.tick(row["window_start"]), clock.tick(row["window_end"])
                if (v not in life or t < last_t or t != right or right - left != step or
                        left != previous.get(v, life[v][0]) or t > life[v][1]):
                    raise ValueError(f"missing/invalid control window {v} at {t}")
                last_t = previous[v] = t
                raw, filtered, rate, weight = [number(row[x]) for x in ("C", "Cbar", "r", "W")]
                busy = number(row["busy_duration_s"])
                if (not 0 <= raw <= 1 or not 0 <= filtered <= 1 or
                        not min_rate - 1e-9 <= rate <= max_rate + 1e-9 or
                        not 1 - 1e-9 <= weight <= 1 + c["beta"] + 1e-9 or
                        busy < -tol or busy > step * clock.sec + tol or
                        abs(raw - busy/(step*clock.sec)) > 1e-8):
                    raise ValueError(f"invalid CBR, rate or weight for {v} at {t}")
                expected = raw if v not in prev_cbar else smoothing * prev_cbar[v] + (1-smoothing)*raw
                if abs(filtered - expected) > 1e-8:
                    raise ValueError(f"inconsistent smoothed CBR for {v} at {t}")
                prev_cbar[v] = filtered
                if real:
                    if fixed_rate > 0 and v not in target_ids:
                        # v4: background vehicles hold a fixed rate and bypass the feedback law.
                        if (abs(weight - 1) > 1e-12 or abs(rate - fixed_rate) > 1e-12
                                or abs(number(row["b"]) - fixed_rate) > 1e-12):
                            raise ValueError(f"background vehicle {v} violates the fixed-rate deployment at {t}")
                        previous_b[v] = fixed_rate
                    else:
                        previous_b[v] = audit_controller(row, cfg["scheme"], c, filtered,
                                                         previous_b.get(v, c["b_initial_hz"]),
                                                         ESTIMATE_SETTLE_S)
                    formula_count += 1
                audit.advance(t)
                if life[v][4]:
                    error_s = abs(busy - audit.integral(v, t) * clock.sec)
                    if error_s > tol:
                        raise ValueError(f"busy integral mismatch {v} at {t}")
                    max_error = max(max_error, error_s)
                    count += 1
                if v in tracked:
                    controls[v].append((t, raw, filtered, rate, weight))
                d = overlap(left, right, a, b)
                f = overlap(t, min(t + step, life[v][1]), a, b)
                ag = cumulative[v]
                if d:
                    ag[0] += d
                    ag[1] += d * raw
                    ag[2] = max(ag[2], raw)
                    pending.append((v, raw, d))
                    if len(pending) >= 1024:
                        conn.executemany("insert into samples values(?,?,?)", pending)
                        pending.clear()
                ag[3] += f
                if rate >= max_rate - 1e-9:
                    ag[4] += f
            audit.advance()
            if set(audit.state) != {v for v, record in life.items() if record[4]}:
                raise ValueError("missing initial audit record for audited vehicle")
            waived = []
            if real:
                diagnostic = json.loads((logs / "sumo_diagnostics.json").read_text(encoding="utf-8-sig"))
                arrivals = completed_trips(logs.parent, diagnostic)
                for v, (ent, leave, *_rest) in life.items():
                    cancelled = check_final_control(v, leave*clock.res, previous.get(v,ent)*clock.res,
                                          step*clock.res, Decimal(str(cfg["timing"]["motion_step_s"])),
                                          clock.res, arrivals)
                    if cancelled:
                        waived.append(v)
            else:
                for v, (ent, leave, *_rest) in life.items():
                    if previous.get(v, ent) + step < leave:
                        raise ValueError(f"missing final full control window for {v}")
            conn.executemany("insert into samples values(?,?,?)", pending)
            ordered = conn.execute("select vehicle,c,exposure from samples order by vehicle,c")
            quantiles = {}
            for v, group in groupby(ordered, key=lambda entry: entry[0]):
                acc = 0
                for _, raw, weight in group:
                    acc += weight
                    if 20*acc >= 19*cumulative[v][0]:
                        quantiles[v] = raw
                        break
        finally:
            conn.close()
    network = {}
    for v, (ent, leave, sent, received, audited) in life.items():
        n, raw_sum, peak, rate_n, saturated = cumulative[v]
        network[v] = {"lifetime_s": (leave-ent)*clock.sec, "submissions_lifetime": sent,
                      "receives_lifetime": received, "cbr_exposure_s": n*clock.sec,
                      "mean_cbr": raw_sum/n if n else None, "p95_cbr": quantiles.get(v),
                      "peak_cbr": peak if n else None, "rate_exposure_s": rate_n*clock.sec,
                      "saturated_fraction": saturated/rate_n if rate_n else None}
    return controls, network, {"audited_windows": count, "max_discrepancy_s": max_error,
                               "controller_formula_windows": formula_count,
                               "final_control_cancelled_on_normal_arrival": sorted(waived)}


def tx_metrics(logs, clock, cfg, life, tracked_senders, masks, window, post, grid):
    a, b = window
    last_t = -1
    seqs, previous, seen, totals = {}, {}, Counter(), defaultdict(lambda: [0, 0, 0])
    real = cfg.get("data_origin", "simulation") == "simulation"
    low, high = (clock.tick(1/cfg["control"][k]) for k in ("r_max_hz", "r_min_hz"))
    for row in rows(logs / "tx_log.csv", "sender_id seq t_submit"):
        v, seq, t = row["sender_id"], int(row["seq"]), clock.tick(row["t_submit"])
        if (v not in life or not life[v][0] <= t <= life[v][1] or
                t < last_t or seq < 0 or seq <= seqs.get(v, -1)):
            raise ValueError(f"unexpected/non-monotonic submission {v}/{seq}")
        if real:
            prior = previous.get(v, life[v][0])
            # A trimmed log may omit transmissions for a nontracked node. First phase and
            # consecutive sequence numbers are the only comparable intervals in such a log.
            if ((seq == 0 or (v in seqs and seq == seqs[v]+1)) and not low-1 <= t-prior <= high+1):
                raise ValueError(f"submission interval/first phase outside frequency limits: {v}/{seq}")
        last_t = t
        previous[v] = t
        seqs[v] = seq
        seen[v] += 1
        if a <= t < b:
            totals[v][0] += 1
            totals[v][1] += t >= post
            if v in masks:
                totals[v][2] += masks[v][(t//grid)*grid][0]
    for v in tracked_senders:
        if seen[v] != life[v][2]:
            raise ValueError(f"registered sender TX count differs from lifecycle: {v}")
    if real:
        for v, (enter, leave, count, _, _) in life.items():
            if seen[v] == count and leave-previous.get(v, enter) > high+1:
                raise ValueError(f"sender exceeded maximum submission interval before exit: {v}")
    return totals


def group_series(controls, vehicles, a, b, step, field, past_window):
    """Piecewise equal-vehicle C or Cbar with exact asynchronous control boundaries."""
    schedules = defaultdict(dict)
    for v in vehicles:
        for record in controls[v]:
            t = record[0] - step if past_window else record[0]
            if t + step <= a or t >= b:
                continue
            schedules[max(a, t)][v] = record[field]
            if t + step < b:
                schedules[t+step].setdefault(v, None)
    cutoffs = sorted({a, b, *schedules})
    active = {}
    series = []
    for start, end in zip(cutoffs, cutoffs[1:]):
        for v, x in schedules.get(start, {}).items():
            if x is None:
                active.pop(v, None)
            else:
                active[v] = x
        if len(active) != len(vehicles):
            raise ValueError("target group C/CBR grid has an uncovered interval")
        series.append((start, end, sum(active[v] for v in sorted(active))/len(active)))
    return series


def recovery(series, *, start, stop, warm, endpoint, confirm, target, ceiling, clock):
    if not start <= stop <= endpoint or confirm <= 0:
        raise ValueError("invalid S2 recovery window/stop time")
    early, late = warm - clock.tick(5), warm - clock.tick(1)
    if early < 0 or not series or series[0][0] > early or series[-1][1] < endpoint:
        raise ValueError("recovery series lacks required baseline/observation exposure")
    expected = series[0][0]
    ok_baseline = ok_last = above = above_limit = 0
    run_start = recovered = None
    for left, right, value in series:
        if left != expected or right <= left:
            raise ValueError("gap/overlap in S2 recovery series")
        expected = right
        if value <= ceiling:
            ok_baseline += overlap(left, right, early, warm)
            ok_last += overlap(left, right, late, warm)
        dt = overlap(left, right, start, endpoint)
        if value > target:
            above += dt
        if value > ceiling:
            above_limit += dt
        l, r = max(left, stop), min(right, endpoint)
        if l < r:
            if value <= ceiling:
                if run_start is None:
                    run_start = l
                if recovered is None and r-run_start >= confirm:
                    recovered = run_start
            else:
                run_start = None
    eligible = ok_baseline*100 >= (warm-early)*95 and ok_last == warm-late
    if not eligible:
        status = "pre_baseline_not_eligible"
    elif not above_limit:
        status = "never_exceeded"
    elif recovered is None:
        status = "not_recovered_by_end"
    else:
        status = "recovered"
    return {"status": status, "baseline_eligible": eligible, "last_stop_s": stop*clock.sec,
            "right_censored": status == "not_recovered_by_end", "above_target_s": above*clock.sec,
            "above_ceiling_s": above_limit*clock.sec,
            "delay_s": (recovered-stop)*clock.sec if status == "recovered" else None,
            "confirmation_s": (recovered+confirm)*clock.sec if status == "recovered" else None}


def h2_control(series, mask, window, post, grid, period, max_rate, clock):
    a, b = window
    totals = {label: [0, 0., 0., 0] for label in ("full", "post", "active")}
    for t, _raw, _bar, rate, weight in series:
        cursor, limit = max(a,t), min(b,t+period)
        while cursor < limit:
            gridpoint = (cursor//grid)*grid
            next_t = min(limit, gridpoint+grid)
            for k, dt in (("full", next_t-cursor), ("post", overlap(cursor,next_t,post,b)),
                          ("active", next_t-cursor if mask[gridpoint][0] else 0)):
                total = totals[k]
                total[0] += dt
                total[1] += dt*weight
                total[2] += dt*rate
                total[3] += dt*(rate >= max_rate-1e-9)
            cursor = next_t
    if totals["full"][0] != b-a:
        raise ValueError("incomplete control/rate coverage for H2")
    return {k: {"exposure_s": d*clock.sec, "mean_W": w/d if d else None,
                "mean_r_hz": r/d if d else None, "saturated_fraction": sat/d if d else None}
            for k,(d,w,r,sat) in totals.items()}


def analyze_state(attempt: Path, cfg: dict, events: dict) -> dict:
    """Validate one completed attempt; return JSON-safe exposure and resource metrics."""
    logs, inputs = Path(attempt)/"logs", Path(attempt)/"inputs"
    timing, ctl = cfg["timing"], cfg["control"]
    clock = Clock(timing)
    tick, sec = clock.tick, clock.sec
    duration, warm, steady = (tick(timing[k]) for k in ("duration_s","warmup_s","stationary_eval_s"))
    grid, step = tick(timing["truth_step_s"]), tick(timing["control_step_s"])
    event_scene = cfg["scenario"] in ("S1","S2")
    start = tick(events["earliest_t0_s"]) if event_scene else warm
    length = tick(timing["post_event_s"]) if event_scene else steady
    end = start + length
    post = min(end, start + tick(timing["observation_s"]))
    if min(grid, step, length) <= 0 or not 0 <= start < end <= duration:
        raise ValueError("invalid or truncated state analysis window")
    window = (start, end)
    life = load_lifecycle(logs, tick, duration)
    pairs = load_pairs(inputs, tick, life, window)
    tracked = {v for p in pairs for v in p}
    targets = [p for p, (_, role) in pairs.items() if role == "target"]
    truth = load_truth(logs, clock, life, tracked, grid, start, end)
    if cfg["scenario"] == "S0":
        stationary_truth(inputs, clock, targets, truth, window)
    h = tick(ctl["lookback_s"])
    masks = {}
    if event_scene:
        for sender in {s for s,_ in targets}:
            masks[sender] = motion_mask(truth[sender], clock, grid, h, ctl["error_scale_m"],
                                        cfg["motion"]["brake_threshold_mps2"], cfg["analysis"].get("h2_tolerance",1e-6))
    state = state_metrics(logs, clock, pairs, life, truth, masks, window, post, cfg)
    controls, network, audit = aggregate_controls(logs, clock, cfg, life, tracked, window, step, ctl.get("smoothing_old",.8))
    tx = tx_metrics(logs, clock, cfg, life, {s for s,_ in pairs}, masks, window, post, grid)
    receivers = sorted({r for _,r in targets})
    receiver_c = group_series(controls, receivers, start, end, step, 1, True)
    cbar_resources = {"target_receivers": receivers, "mean_cbr_equal_vehicles": mean([network[v]["mean_cbr"] for v in receivers]),
                      "group_series_p95_cbr": weighted_p95([(val, r-l) for l,r,val in receiver_c]),
                      "group_series_peak_cbr": max(val for _,_,val in receiver_c)}
    results, h2, hashes = {}, {}, {}
    for p, (pair_id, role) in pairs.items():
        s, r = p
        results[pair_id] = {"sender_id":s,"receiver_id":r,"role":role, **state[p]["full"].result(sec)}
        if event_scene and role == "target":
            hh = h2_control(controls[s], masks[s], window, post, grid, step, ctl["r_max_hz"], clock)
            for i, label in enumerate(("full","post","active")):
                hh[label].update(state[p][label].result(sec))
                hh[label]["submissions"] = tx[s][i]
                d = hh[label]["exposure_s"]
                hh[label]["submission_rate_hz"] = tx[s][i]/d if d else None
            hh["active_status"] = "observed" if hh["active"]["exposure_s"] else "insufficient_mechanism_exposure"
            h2[pair_id] = hh
    for v, table in truth.items():
        digest = hashlib.sha256()
        for t,(x,speed) in sorted(table.items()):
            digest.update(f"{t},{x.hex()},{speed.hex()}\n".encode("ascii"))
        hashes[v] = digest.hexdigest()
    s2 = None
    if cfg["scenario"] == "S2":
        left, right = warm-tick(5), warm+steady
        if events.get("last_stop_s") is None or right > duration:
            raise ValueError("S2 physical stop or recovery window missing")
        kw = dict(start=start,stop=tick(events["last_stop_s"]),warm=warm,endpoint=right,
                  confirm=tick(timing["recovery_confirm_s"]),target=ctl["cbr_target"],
                  ceiling=ctl["cbr_target"]+ctl["deadband"],clock=clock)
        by_vehicle = {v: recovery(group_series(controls,[v],left,right,step,2,False),**kw) for v in receivers}
        s2 = {"group":recovery(group_series(controls,receivers,left,right,step,2,False),**kw),
              "per_receiver":by_vehicle}
    for v in {s for s, _ in pairs}:
        network[v].update({"submissions_in_window":tx[v][0], "submission_rate_hz":tx[v][0]/(length*sec)})
    return {"evaluation_start_s":start*sec, "evaluation_end_s":end*sec,
            "trajectory_sha256_by_vehicle":hashes, "pairs":results, "h2":h2, "s2_recovery":s2,
            "vehicles":network, "target_cbr":cbar_resources, "busy_audit":audit,
            "whole_network":{"vehicles":len(life),"submissions_lifetime":sum(v[2] for v in life.values()),
                             "receives_lifetime":sum(v[3] for v in life.values()),
                             "vehicle_seconds":sum(v[1]-v[0] for v in life.values())*sec}}
