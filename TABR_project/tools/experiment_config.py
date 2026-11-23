"""Configuration loading shared by the TABR CLI, engine and analysis."""
import json
import math
from pathlib import Path

from jsonschema import Draft202012Validator


PROJECT = Path(__file__).resolve().parents[1]
SCHEMA = PROJECT / "configs" / "experiment.schema.json"


def validate_config(cfg):
    schema = json.loads(SCHEMA.read_text(encoding="utf-8-sig"))
    for error in Draft202012Validator(schema).iter_errors(cfg):
        path = ".".join(str(item) for item in error.absolute_path)
        raise ValueError(f"Invalid configuration {path}: {error.message}")
    timing, control = cfg["timing"], cfg["control"]
    if not timing["deadline_s"] <= timing["observation_s"] <= timing["post_event_s"]:
        raise ValueError("Deadline, event window, and post-event window must be ordered")
    if timing["truth_step_s"] != timing["motion_step_s"]:
        raise ValueError("Truth grid must coincide with motion updates in the core protocol")
    if timing["warmup_s"] + max(timing["stationary_eval_s"], timing["post_event_s"]) > timing["duration_s"]:
        raise ValueError("The evaluation does not fit in the simulated time")
    for key in ("motion_step_s", "control_step_s", "truth_step_s", "warmup_s"):
        ticks = timing[key] / timing["time_resolution_s"]
        if not math.isclose(ticks, round(ticks), rel_tol=0, abs_tol=0.01):
            raise ValueError(f"Timing value is not representable in SimTime ticks: {key}")
    if control["smoothing_old"] >= 1 or control["jitter_fraction"] >= 1:
        raise ValueError("Smoothing weight and jitter must be less than one")
    if control["r_min_hz"] > control["r_max_hz"] or not math.isclose(control["b_min_hz"], control["r_min_hz"] / (1 + control["beta"])):
        raise ValueError("Inconsistent control frequency limits")
    if not control["b_min_hz"] <= control["b_initial_hz"] <= control["b_max_hz"]:
        raise ValueError("Initial base rate outside configured limits")
    if cfg["wireless"]["message_bits"] <= 0 or cfg["wireless"]["bitrate_bps"] <= 0:
        raise ValueError("Message length and bitrate must be positive")
    geom = cfg["geometry"]
    if not 0 < geom["target_x_m"] < geom["length_m"] or not 0 < geom["pair_min_distance_m"] <= geom["pair_max_distance_m"]:
        raise ValueError("Invalid target position or background pair distance limits")
    seeds = cfg["seeds"]
    all_seeds = [*seeds["formal"], *seeds["calibration"], *seeds["pilot"], seeds["acceptance"]]
    if len(all_seeds) != len(set(all_seeds)):
        raise ValueError("Formal, acceptance, calibration and pilot seeds must be disjoint")
    if cfg["stage"] == "frozen" and cfg["loads"]["low"]["density_per_km_lane"] >= cfg["loads"]["high"]["density_per_km_lane"]:
        raise ValueError("Frozen low and high density must be distinct and ordered")
    missing = [k for k, value in cfg["tools"].items() if not Path(value).exists()]
    if missing:
        raise ValueError("Missing configured tool paths: " + ", ".join(missing))
    return cfg


def load_config(path):
    return validate_config(json.loads(Path(path).read_text(encoding="utf-8-sig")))
