"""Execution-only first-attempt budgets from completed, comparable measurements."""

import math


def positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return value


def slot_count(value):
    if type(value) is not int or value < 1:
        raise ValueError("slots must be a positive integer")
    return value


def select_initial_budget(benchmark_runs, *, density, duration_s, slots, authorized_cap_s,
                          machine_id, cost_group, fallback_s=0):
    """Select the first-attempt wall timeout, bounded by a caller-supplied authorization.

    Rows have attempt_id, machine_id, cost_group, accepted, density, duration_s,
    slots and wall_seconds. The caller supplies provenance (same host and comparable engine,
    radio policy, scenario and logging); an interrupted run is not a duration measurement.
    fallback_s is an optional *floor*, never the actual ceiling.
    """
    positive(density, "density")
    positive(duration_s, "duration_s")
    slot_count(slots)
    if type(authorized_cap_s) is not int or authorized_cap_s <= 0:
        raise ValueError("An explicit positive integer authorized_cap_s is required")
    if type(fallback_s) is not int or fallback_s < 0:
        raise ValueError("fallback_s must be a nonnegative integer")
    if not machine_id or not cost_group:
        raise ValueError("machine_id and cost_group are required")

    candidates = {}
    for row in benchmark_runs:
        if row["accepted"] is not True or (row["machine_id"],row["cost_group"]) != (machine_id,cost_group):
            continue
        d = positive(row["density"], "historical density")
        duration = positive(row["duration_s"], "historical duration")
        if d < density or duration < duration_s:
            continue
        slot_count(row["slots"])
        positive(row["wall_seconds"], "historical wall seconds")
        key = row["attempt_id"]
        if not isinstance(key,str) or not key:
            raise ValueError("Missing closed attempt identity")
        record = (d, duration, row["slots"], row["wall_seconds"])
        if key in candidates and candidates[key] != record:
            raise ValueError(f"Conflicting benchmark for attempt {key}")
        candidates[key] = record

    result = dict(requested_density=density, requested_duration_s=duration_s, requested_slots=slots,
                  machine_id=machine_id, cost_group=cost_group, authorized_cap_s=authorized_cap_s,
                  fallback_floor_s=fallback_s, benchmark_attempt_ids=[], basis="unmeasured_size_or_horizon",
                  budget_before_cap_s=None, wall_timeout_s=authorized_cap_s,
                  benchmark_exceeds_cap=None, concurrency_estimated=None, estimated_wall_s=None,
                  floor_exceeds_cap=fallback_s>authorized_cap_s)
    if not candidates:
        return result

    # Use full measured wall time. No shorter-than-planned reference durations, linear
    # interpolation across nonlinear densities or optimistic duration rescaling.
    d, horizon = min((row[0],row[1]) for row in candidates.values())
    at_scale = {key: row for key,row in candidates.items() if row[:2] == (d,horizon)}
    measured_slots = sorted({row[2] for row in at_scale.values()})
    reference_slots = next((s for s in measured_slots if s >= slots), measured_slots[-1])
    records = {key:row[3] for key,row in at_scale.items() if row[2] == reference_slots}
    slowest = max(records.values())
    # Without a closed measurement at the requested concurrency, budget for nominal
    # serialization of the extra slots. This is a planning assumption, not a guarantee.
    multiplier = max(1.0, slots/reference_slots)
    estimate = slowest * multiplier
    before_cap = max(fallback_s, 60*math.ceil((1.25*estimate + 60)/60))
    result.update(basis="same_density" if d==density else "nearest_heavier_density",
                  reference_density=d, reference_duration_s=horizon, reference_slots=reference_slots,
                  benchmark_attempt_ids=sorted(records), slowest_wall_s=slowest,
                  concurrency_factor=multiplier, concurrency_estimated=slots>reference_slots,
                  estimated_wall_s=estimate, budget_before_cap_s=before_cap,
                  wall_timeout_s=min(before_cap,authorized_cap_s), benchmark_exceeds_cap=before_cap>authorized_cap_s)
    return result
