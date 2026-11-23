"""Generate immutable uniform-traffic SUMO/Veins inputs for a TABR attempt."""

import csv
import argparse
from bisect import bisect_right
from collections import defaultdict
import hashlib
import json
import math
import socket
import sys
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET


class ScenarioGeometryError(ValueError):
    """Preregistered geometry cannot furnish the required observation pairs."""

    def __init__(self, report):
        self.report = report
        super().__init__(report["reason"])


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields.split(), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_xml(path, root):
    ET.indent(root)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def number(value):
    return format(value, ".12g")


def phase_uniform(*parts):
    message = "|".join(str(x) for x in parts).encode("ascii")
    return int.from_bytes(hashlib.sha256(message).digest()[:8], "big") / (1 << 64)


def vehicle(name, role, road, when, pos, speed):
    return {
        "vehicle_id": name,
        "role": role,
        "road_id": road,
        "lane_id": road + "_0",
        "depart_s": when,
        "depart_position_m": pos,
        "depart_speed_mps": speed,
        "prefill": int(when == 0),
    }


def background(cfg, job, duration, target_start, speed, step):
    geom = cfg["geometry"]
    length, lanes = float(geom["length_m"]), int(geom["background_lanes"])
    count = job.get("background_count")
    resource_probe = job.get("stage") == "resource_only"
    if count is not None and not resource_probe and job.get("stage") != "engineering_geometry":
        raise ValueError("background_count override belongs only to resource-only runs")
    density = job.get("density_per_km_lane")
    if density is None and job.get("load_id") in cfg["loads"]:
        density = cfg["loads"][job["load_id"]]["density_per_km_lane"]
    if count is None:
        if density is None or float(density) <= 0:
            raise ValueError("A positive, resolved background density is required")
        exact = float(density) * length / 1000
        if not math.isclose(exact, round(exact), abs_tol=1e-9):
            raise ValueError("Uniform prefill requires an integral vehicle count per lane")
        per_lane = round(exact)
    else:
        if isinstance(count, bool) or int(count) != count or int(count) <= 0 or int(count) % lanes:
            raise ValueError("Resource count must be positive and divisible by the lane count")
        per_lane = int(count) // lanes
    short = (resource_probe or job.get("stage") == "engineering_geometry") and count in (16, 64)
    pitch = 150.0 if short else length / per_lane
    if pitch < cfg["motion"]["vehicle_length_m"] + 2.5 + speed:
        raise ValueError("Uniform spacing is below the configured Krauss cruise headway")
    members = []
    origin = 5.0
    seed = int(job["seed"])
    for lane in range(lanes):
        road = f"bg{lane}"
        phase = phase_uniform("traffic-position", seed, road)
        if short:
            start = target_start - (per_lane - 1) * pitch / 2 + (phase - 0.5) * pitch / 2
        else:
            # Keep both road ends clear; pitch is exactly 1000/density, including sparse rejected candidates.
            start = origin + phase * (pitch - 2 * origin)
        for i in range(per_lane):
            pos = start + i * pitch
            if not origin <= pos <= length - origin:
                raise ValueError(f"Prefilled vehicle outside road {road}: {pos}")
            members.append(vehicle(f"{road}_p{i:04d}", "background", road, 0, pos, speed))
        if short:
            # This bounded moving segment tests handles/peak population, not steady full-road offered load.
            continue
        for j in range(1, math.ceil(duration * speed / pitch) + 2):
            virtual = start - j * pitch
            crossing = (origin - virtual) / speed
            tick = max(1, math.ceil(crossing / step - 1e-10))
            when = tick * step
            if when >= duration - step / 2:
                break
            x = virtual + speed * when
            members.append(vehicle(f"{road}_u{j:04d}", "background", road, when, x, speed))
    return members, {
        "uniform_spacing_m": pitch,
        "background_prefill_n": per_lane * lanes,
        "density_per_km_lane": None if short else per_lane * 1000 / length,
        "distribution": "uniform_short_segment" if short else "uniform_full_road",
        "stage": job.get("stage", "core"),
    }


def registrations(cfg, job):
    timing, geom, motion = cfg["timing"], cfg["geometry"], cfg["motion"]
    scenario, seed = job["scenario"], int(job["seed"])
    if scenario not in cfg["scenarios"] or seed < 0:
        raise ValueError("Unknown scenario or negative seed")
    resource = job.get("stage") == "resource_only"
    duration = float(
        job.get("resource_duration_s", timing["duration_s"]) if resource else timing["duration_s"]
    )
    step, warmup = float(timing["motion_step_s"]), float(timing["warmup_s"])
    speed = float(motion["speed_kmh"]) / 3.6
    moving = scenario != "S0"
    if resource and scenario in ("S1", "S2") and duration <= warmup + timing["post_event_s"] + step:
        raise ValueError("Use E0 for sub-event-duration resource probes")
    if resource:
        begin, end = step, duration
    else:
        begin = warmup
        end = warmup + (
            timing["stationary_eval_s"] if scenario in ("E0", "S0") else timing["post_event_s"] + step
        )
    if end > duration or end <= begin:
        raise ValueError("Requested run cannot cover the full evaluation window")
    center, length = float(geom["target_x_m"]), float(geom["length_m"])
    target_start = center - (max(0, warmup - step) * speed if moving and not resource else 0)
    count = int(cfg["scenarios"][scenario]["target_pairs"])
    vehicles, pairs, events, priors = [], [], [], []
    for i in range(count):
        lead, follow = ("lead", "follow") if count == 1 else (f"lead{i}", f"follow{i}")
        lead_pos, follow_pos = target_start, target_start - motion["vehicle_length_m"] - motion["net_gap_m"]
        if not 5 <= follow_pos < lead_pos < length:
            raise ValueError("Target initial positions do not fit the road")
        road = f"target{i}"
        for vid, position in ((lead, lead_pos), (follow, follow_pos)):
            vehicles.append(vehicle(vid, "target", road, 0, position, speed if moving else 0.0))
        event_id = f"{lead}-brake-1" if scenario in ("S1", "S2") else ""
        pairs.append(
            {
                "pair_id": f"{lead}->{follow}",
                "sender_id": lead,
                "receiver_id": follow,
                "role": "target",
                "eval_start": begin,
                "eval_end": end,
                "planned_event_id": event_id,
            }
        )
        if event_id:
            events.append(
                {
                    "planned_event_id": event_id,
                    "sender_id": lead,
                    "planned_time_s": warmup,
                    "target_speed_mps": 0,
                    "brake_duration_s": speed / motion["deceleration_mps2"],
                    "deadline_s": timing["deadline_s"],
                    "observation_s": timing["observation_s"],
                }
            )
        if scenario == "S0":
            priors.append(
                {
                    "receiver_id": follow,
                    "sender_id": lead,
                    "init_time": warmup,
                    "prior_gen_time": warmup,
                    "position_m": lead_pos,
                    "speed_mps": 0,
                    "source": "given_prior",
                }
            )
    bg, spacing = background(cfg, job, duration, target_start, speed, step)
    candidates = []
    excluded_window = excluded_distance = scanned = 0
    target_at_begin = target_start + (speed * max(0, begin - step) if moving else 0.0)
    for road in sorted({v["road_id"] for v in bg}):
        local = sorted(
            (v for v in bg if v["road_id"] == road and v["prefill"]), key=lambda v: v["depart_position_m"]
        )
        for rear, front in zip(local, local[1:]):
            p0, p1 = rear["depart_position_m"], front["depart_position_m"]
            scanned += 1
            if p1 + speed * max(0, end - step) >= length:
                excluded_window += 1
                continue
            if not geom["pair_min_distance_m"] <= p1 - p0 <= geom["pair_max_distance_m"]:
                excluded_distance += 1
                continue
            midpoint = (p1 + p0) / 2 + speed * max(0, begin - step)
            candidates.append(
                (abs(midpoint - target_at_begin), front["lane_id"], front["vehicle_id"], rear["vehicle_id"])
            )
    needed = int(geom["background_pair_count"])
    if len(candidates) < needed:
        raise ScenarioGeometryError(
            {
                **spacing,
                "scenario": scenario,
                "seed": seed,
                "valid": False,
                "reason": f"Only {len(candidates)} eligible adjacent full-window background pairs; need {needed}",
                "eligible_background_pairs": len(candidates),
                "required_background_pairs": needed,
                "source": "formal_scenario.registrations",
                "data_origin": "synthetic" if cfg.get("synthetic") is True else "scenario_preregistration",
                "simulation_started": False,
                "speed_mps": speed,
                "speed_kmh": motion["speed_kmh"],
                "config_geometry": {key: geom[key] for key in ("length_m", "background_lanes",
                                    "pair_min_distance_m", "pair_max_distance_m", "background_pair_count")},
                "selection_window_s": [begin, end],
                "complete_adjacent_pairs_scanned": scanned,
                "filtering": {"eligible_pairs": len(candidates), "excluded_outside_window": excluded_window,
                              "excluded_distance": excluded_distance, "disjoint_counts": True},
            }
        )
    for _, lane, front, rear in sorted(candidates)[:needed]:
        pairs.append(
            {
                "pair_id": f"{front}->{rear}",
                "sender_id": front,
                "receiver_id": rear,
                "role": "background",
                "eval_start": begin,
                "eval_end": end,
                "planned_event_id": "",
            }
        )
    vehicles += bg
    phases = [
        {
            "vehicle_id": v["vehicle_id"],
            "phase_s": 1 / cfg["control"]["r_max_hz"]
            + (1 / cfg["control"]["r_min_hz"] - 1 / cfg["control"]["r_max_hz"])
            * phase_uniform("initial-phase", seed, v["vehicle_id"]),
        }
        for v in vehicles
    ]
    return (
        vehicles,
        pairs,
        events,
        priors,
        phases,
        {
            **spacing,
            "scenario": scenario,
            "seed": seed,
            "duration_s": duration,
            "eligible_background_pairs": len(candidates),
            "target_count": 2 * count,
            "vehicles_registered": len(vehicles),
            "begin_evaluation_s": begin,
            "end_evaluation_s": end,
            "coordinate": "SUMO front-bumper road position, Veins margin=0 and x-min=0",
            "first_mobility_tick_s": step,
        },
    )


def model_files(cfg, destination, vehicles, meta):
    nodes, edges, routes = ET.Element("nodes"), ET.Element("edges"), ET.Element("routes")
    motion, timing = cfg["motion"], cfg["timing"]
    cruise = motion["speed_kmh"] / 3.6
    roads = [f"target{i}" for i in range(4)] + [f"bg{i}" for i in range(cfg["geometry"]["background_lanes"])]
    for i, road in enumerate(roads):
        for end, x in (("w", 0), ("e", cfg["geometry"]["length_m"])):
            ET.SubElement(nodes, "node", id=road + end, x=number(x), y=number(10 + i * 4), type="priority")
        # netconvert 1.6 prints lane limits to two decimals. The original road's 120 km/h ceiling
        # allows SUMO to insert the exact 80 km/h vehicle speed fixed separately by vType.
        ET.SubElement(
            edges,
            "edge",
            id=road,
            attrib={
                "from": road + "w",
                "to": road + "e",
                "numLanes": "1",
                "speed": number(max(120 / 3.6, cruise + 0.01)),
            },
        )
    for kind in ("target", "background"):
        ET.SubElement(
            routes,
            "vType",
            id=kind,
            accel="2.6",
            decel=number(motion["deceleration_mps2"]),
            emergencyDecel="9",
            sigma="0",
            tau="1",
            length=number(motion["vehicle_length_m"]),
            minGap="2.5",
            maxSpeed=number(cruise),
            speedFactor="1",
            speedDev="0",
        )
    for road in roads:
        ET.SubElement(routes, "route", id=road, edges=road)
    for v in sorted(
        vehicles, key=lambda u: (u["depart_s"], u["road_id"], -u["depart_position_m"], u["vehicle_id"])
    ):
        car = ET.SubElement(
            routes,
            "vehicle",
            id=v["vehicle_id"],
            type=v["role"],
            route=v["road_id"],
            depart=number(v["depart_s"]),
            departLane="0",
            departPos=number(v["depart_position_m"]),
            departSpeed=number(v["depart_speed_mps"]),
        )
        if meta["scenario"] == "S0" and v["role"] == "target":
            ET.SubElement(
                car,
                "stop",
                lane=v["lane_id"],
                endPos=number(v["depart_position_m"]),
                duration=number(meta["duration_s"] + 1),
            )
    write_xml(destination / "highway.nod.xml", nodes)
    write_xml(destination / "highway.edg.xml", edges)
    write_xml(destination / "traffic.rou.xml", routes)
    cmd = [
        str(Path(cfg["tools"]["sumo_bin"]) / "netconvert.exe"),
        "--node-files",
        "highway.nod.xml",
        "--edge-files",
        "highway.edg.xml",
        "--output-file",
        "highway.net.xml",
        "--offset.disable-normalization",
        "true",
        "--no-internal-links",
        "true",
    ]
    result = subprocess.run(
        cmd,
        cwd=destination,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        capture_output=True,
        timeout=60,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode:
        raise ValueError(f"netconvert exited {result.returncode}: {result.stderr.strip()}")
    # netconvert's version banner comment embeds wall time and generated absolute paths. The XML root is the model.
    write_xml(destination / "highway.net.xml", ET.parse(destination / "highway.net.xml").getroot())
    model = ET.Element("configuration")
    for group, values in (
        ("input", {"net-file": "highway.net.xml", "route-files": "traffic.rou.xml"}),
        (
            "time",
            {"begin": "0", "end": number(meta["duration_s"]), "step-length": number(timing["motion_step_s"])},
        ),
        (
            "processing",
            {"time-to-teleport": "-1", "collision.action": "warn", "collision.mingap-factor": "1"},
        ),
        (
            "output",
            {
                "summary-output": "summary.xml",
                "tripinfo-output": "trips.xml",
                "tripinfo-output.write-unfinished": "true",
            },
        ),
        ("report", {"no-step-log": "true", "duration-log.disable": "true"}),
    ):
        sub = ET.SubElement(model, group)
        for tag, value in values.items():
            ET.SubElement(sub, tag, value=value)
    write_xml(destination / "traffic.sumo.cfg", model)
    launch = ET.Element("launch")
    ET.SubElement(launch, "basedir", path=destination.resolve().as_posix())
    ET.SubElement(launch, "copy", file="highway.net.xml")
    ET.SubElement(launch, "copy", file="traffic.rou.xml")
    ET.SubElement(launch, "copy", file="traffic.sumo.cfg", type="config")
    write_xml(destination / "traffic.launchd.xml", launch)


def prepare_inputs(cfg, job, destination):
    """Preregister the exact scenario and return its portable file list and geometry metadata."""
    job = resolved_geometry_job(cfg, job)
    destination = Path(destination)
    destination.mkdir(exist_ok=True, parents=True)
    files = (
        "events.csv",
        "pairs.csv",
        "prior.csv",
        "vehicles.csv",
        "phases.csv",
        "highway.nod.xml",
        "highway.edg.xml",
        "highway.net.xml",
        "traffic.rou.xml",
        "traffic.sumo.cfg",
        "traffic.launchd.xml",
        "scenario.json",
    )
    if any((destination / name).exists() for name in files):
        raise ValueError(f"Refusing to overwrite prepared scenario inputs: {destination}")
    try:
        vehicles, pairs, events, priors, phases, meta = registrations(cfg, job)
    except ScenarioGeometryError as error:
        write_json(destination / "scenario.json", error.report)
        raise
    write_csv(
        destination / "events.csv",
        "planned_event_id sender_id planned_time_s target_speed_mps brake_duration_s deadline_s observation_s",
        events,
    )
    write_csv(
        destination / "pairs.csv",
        "pair_id sender_id receiver_id role eval_start eval_end planned_event_id",
        pairs,
    )
    write_csv(
        destination / "prior.csv",
        "receiver_id sender_id init_time prior_gen_time position_m speed_mps source",
        priors,
    )
    write_csv(
        destination / "vehicles.csv",
        "vehicle_id role road_id lane_id depart_s depart_position_m depart_speed_mps prefill",
        vehicles,
    )
    write_csv(destination / "phases.csv", "vehicle_id phase_s", phases)
    model_files(cfg, destination, vehicles, meta)
    meta.update(files=list(files), valid=True)
    write_json(destination / "scenario.json", meta)
    return meta


def resolved_geometry_job(cfg, job):
    job = dict(job)
    engineering = cfg.get("engineering_geometry")
    probe = cfg.get("resource_probe")
    if engineering and probe:
        raise ValueError("Engineering geometry and resource-only probe cannot both select a geometry")
    if engineering:
        job.update(stage="engineering_geometry", background_count=int(engineering["background_vehicles"]))
    if probe:
        job.update(
            stage="resource_only",
            background_count=int(probe["background_vehicles"]),
            resource_duration_s=float(probe.get("duration_s", cfg["timing"]["duration_s"])),
        )
    return job


def table(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def audit_sumo(cfg, job, inputs, destination):
    """Drive SUMO alone with the same TraCI brakes and independently measure every vehicle."""
    from formal_events import Clock, registrations as event_plans, truth_and_physics

    job = resolved_geometry_job(cfg, job)
    inputs, destination = Path(inputs).resolve(), Path(destination).resolve()
    destination.mkdir(exist_ok=True, parents=True)
    if (destination / "validation.json").exists() or (destination / "scenario_truth.csv").exists():
        raise ValueError(f"Diagnostic directory is already populated: {destination}")
    sim_bin = Path(cfg["tools"]["sumo_bin"])
    sys.path.insert(0, str(sim_bin.parent / "tools"))
    try:
        import traci
        import traci.constants as tc
    finally:
        sys.path.pop(0)
    meta = json.loads((inputs / "scenario.json").read_text(encoding="utf-8"))
    clock = Clock(cfg["timing"]["time_resolution_s"])
    step = float(cfg["timing"]["motion_step_s"])
    count = round(meta["duration_s"] / step)
    registered = {row["vehicle_id"]: row for row in table(inputs / "vehicles.csv")}
    pairs = table(inputs / "pairs.csv")
    tracked = {row[key] for row in pairs for key in ("sender_id", "receiver_id")}
    targets = {vid for vid, row in registered.items() if row["role"] == "target"}
    events = table(inputs / "events.csv")
    departed, arrived, onsets, previous = {}, {}, {}, {}
    command_sent = set()
    held = set()
    anomalies = defaultdict(list)
    min_lane_gap = {}
    prefill = sum(int(row["prefill"]) for row in registered.values())
    peak = 0
    min_pair_gap, max_pair_gap = None, None
    distance_bin_upper_m = [50, 100, 250, 500, 1000, 2000, 5000]
    distance_hist = {vid: [0] * (len(distance_bin_upper_m) + 1) for vid in targets}
    whole_network_truth = hashlib.sha256()
    lane_population_exposure = defaultdict(int)
    gap_violation_ticks = 0
    overlap_ticks = 0
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    exe = sim_bin / "sumo.exe"
    cmd = [
        str(exe),
        "-c",
        str(inputs / "traffic.sumo.cfg"),
        "--seed",
        str(job["seed"]),
        "--remote-port",
        str(port),
        "--summary-output",
        str(destination / "summary.xml"),
        "--tripinfo-output",
        str(destination / "trips.xml"),
    ]
    with (destination / "sumo_console.log").open("wb") as console, (destination / "scenario_truth.csv").open(
        "w", newline=""
    ) as motion, (destination / "local_supply.csv").open("w", newline="") as local:
        motion_csv = csv.writer(motion)
        motion_csv.writerow(["t", "vehicle_id", "position_m", "speed_mps", "accel_mps2"])
        supply_csv = csv.writer(local)
        supply_csv.writerow(["t", "vehicle_id", "background_count"])
        process = subprocess.Popen(
            cmd,
            cwd=destination,
            stdout=console,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        conn = None
        try:
            conn = traci.connect(port, numRetries=50, proc=process, waitBetweenRetries=0.1)
            # One context subscription covers the full straight network. No per-vehicle per-tick RPCs.
            conn.junction.subscribeContext(
                "target0w",
                tc.CMD_GET_VEHICLE_VARIABLE,
                cfg["geometry"]["length_m"] + 100,
                [tc.VAR_LANE_ID, tc.VAR_LANEPOSITION, tc.VAR_SPEED],
            )
            conn.simulation.subscribe(
                [
                    tc.VAR_DEPARTED_VEHICLES_IDS,
                    tc.VAR_ARRIVED_VEHICLES_IDS,
                    tc.VAR_TELEPORT_STARTING_VEHICLES_IDS,
                    tc.VAR_COLLIDING_VEHICLES_IDS,
                ]
            )
            for k in range(1, count + 1):
                conn.simulationStep()
                now = round(k * step, 10)
                frame = conn.junction.getContextSubscriptionResults("target0w") or {}
                sim = conn.simulation.getSubscriptionResults()
                for vid in sim[tc.VAR_DEPARTED_VEHICLES_IDS]:
                    if vid not in registered or vid in departed:
                        raise ValueError(f"Unexpected or duplicate insertion {vid}")
                    # SUMO departs during [now-step,now); OMNeT sees the vehicle at this completed step.
                    departed[vid] = now
                    delay = round(now - step - float(registered[vid]["depart_s"]), 9)
                    if abs(delay) > 1e-6:
                        anomalies["insertion_delay"].append([vid, delay])
                for vid in sim[tc.VAR_ARRIVED_VEHICLES_IDS]:
                    arrived[vid] = now
                for key, var in (
                    ("teleport", tc.VAR_TELEPORT_STARTING_VEHICLES_IDS),
                    ("collision", tc.VAR_COLLIDING_VEHICLES_IDS),
                ):
                    if sim[var]:
                        anomalies[key].append([now, list(sim[var])])
                if set(frame) != (set(departed) - set(arrived)):
                    raise ValueError(f"Context observation differs from active lifecycle registry at t={now}")
                if k == 1 and len(frame) != prefill:
                    raise ValueError(f"Initial frame has {len(frame)}, preregistered {prefill}")
                peak = max(peak, len(frame))
                positions = defaultdict(list)
                background_x = []
                for vid in sorted(frame):
                    values = frame[vid]
                    lane, x, speed = values[tc.VAR_LANE_ID], values[tc.VAR_LANEPOSITION], values[tc.VAR_SPEED]
                    if (
                        not all(map(math.isfinite, (x, speed)))
                        or x < 0
                        or x > cfg["geometry"]["length_m"] + 1e-6
                    ):
                        raise ValueError(f"Invalid independent SUMO state of {vid}")
                    if lane != registered[vid]["lane_id"]:
                        raise ValueError(f"Unexpected lane {lane} for {vid}")
                    positions[lane].append(x)
                    lane_population_exposure[lane] += 1
                    whole_network_truth.update(f"{k}|{vid}|{lane}|{x.hex()}|{speed.hex()}\n".encode("ascii"))
                    if registered[vid]["role"] == "background":
                        background_x.append(x)
                    if vid in tracked:
                        before = previous.get(vid)
                        accel = (speed - before[1]) / step if before else None
                        motion_csv.writerow(
                            [number(now), vid, repr(x), repr(speed), "" if accel is None else repr(accel)]
                        )
                        if vid in command_sent and before and before[1] - speed > 1e-6:
                            onsets.setdefault(vid, (before[0], now))
                        previous[vid] = (now, speed)
                    if meta["scenario"] == "S0" and vid in targets:
                        if speed > 1e-7 or abs(x - float(registered[vid]["depart_position_m"])) > 1e-6:
                            raise ValueError(f"Stationary prior mismatch at {now} for {vid}: x={x} v={speed}")
                for lane, coords in positions.items():
                    coords.sort()
                    for left, right in zip(coords, coords[1:]):
                        gap = right - left - cfg["motion"]["vehicle_length_m"]
                        min_lane_gap[lane] = min(min_lane_gap.get(lane, gap), gap)
                        if gap < 2.5 - 1e-6:
                            gap_violation_ticks += 1
                            overlap_ticks += int(gap < 0)
                            if len(anomalies["minGap_violation_examples"]) < 20:
                                anomalies["minGap_violation_examples"].append([now, lane, gap])
                for vid in sorted(targets):
                    if vid not in frame:
                        raise ValueError(f"Target {vid} absent at t={now}")
                    x = frame[vid][tc.VAR_LANEPOSITION]
                    local_population = 0
                    for bg_x in background_x:
                        separation = abs(bg_x - x)
                        local_population += int(separation <= cfg["geometry"]["local_radius_m"])
                        distance_hist[vid][bisect_right(distance_bin_upper_m, separation)] += 1
                    supply_csv.writerow([number(now), vid, local_population])
                if meta["begin_evaluation_s"] <= now < meta["end_evaluation_s"]:
                    for pair in pairs:
                        front, rear = pair["sender_id"], pair["receiver_id"]
                        if front not in frame or rear not in frame:
                            raise ValueError(f"Registered {pair['pair_id']} absent within evaluation")
                        if pair["role"] == "background":
                            distance = frame[front][tc.VAR_LANEPOSITION] - frame[rear][tc.VAR_LANEPOSITION]
                            min_pair_gap = distance if min_pair_gap is None else min(min_pair_gap, distance)
                            max_pair_gap = distance if max_pair_gap is None else max(max_pair_gap, distance)
                            if (
                                distance < cfg["geometry"]["pair_min_distance_m"] - 1e-5
                                or distance > cfg["geometry"]["pair_max_distance_m"] + 1e-5
                            ):
                                raise ValueError(
                                    f"Background pair leaves registered range: {pair['pair_id']} {distance}m"
                                )
                for event in events:
                    vid, start = event["sender_id"], float(event["planned_time_s"])
                    brake_duration = float(event["brake_duration_s"])
                    if vid not in command_sent and now >= start:
                        conn.vehicle.slowDown(vid, float(event["target_speed_mps"]), brake_duration)
                        command_sent.add(vid)
                    # Veins holdStop is issued between these two SUMO steps when brake duration isn't a multiple of dt.
                    if vid not in held and now <= start + brake_duration < now + step:
                        conn.vehicle.setSpeed(vid, float(event["target_speed_mps"]))
                        held.add(vid)
        finally:
            try:
                if conn:
                    conn.close(wait=False)
            finally:
                if process.poll() is None:
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        process.wait(timeout=10)
    if process.returncode:
        raise ValueError(f"SUMO exited {process.returncode}")
    event_log = []
    for event in events:
        sender = event["sender_id"]
        l, u = onsets.get(sender, (None, None))
        event_log.append(
            {
                "sender_id": sender,
                "planned_event_id": event["planned_event_id"],
                "t0_lower_bound": l,
                "t0": u,
                "event_status": "occurred" if u is not None else "not_occurred",
                "exclusion_reason": "" if u is not None else "no_speed_drop",
            }
        )
    write_csv(
        destination / "event_log.csv",
        "planned_event_id sender_id t0_lower_bound t0 event_status exclusion_reason",
        event_log,
    )
    life = [
        {
            "vehicle_id": vid,
            "planned_depart_s": registered[vid]["depart_s"],
            "first_observed_s": departed.get(vid),
            "normal_exit_s": arrived.get(vid),
            "at_end": int(vid in departed and vid not in arrived),
        }
        for vid in sorted(registered)
    ]
    write_csv(
        destination / "lifecycle.csv",
        "vehicle_id planned_depart_s first_observed_s normal_exit_s at_end",
        life,
    )
    missing = sorted(set(registered) - set(departed))
    if missing:
        anomalies["missing_insertions"] = missing
    geometry_report = {
        **meta,
        "source": "independent_SUMO_only",
        "pid": process.pid,
        "actual_insertions": len(departed),
        "normal_exits": len(arrived),
        "peak_all_vehicles": peak,
        "initial_all_vehicles": prefill,
        "min_net_gap_m_by_lane": min_lane_gap,
        "registered_background_spacing_m": [min_pair_gap, max_pair_gap],
        "anomalies": dict(anomalies),
        "physics": {},
        "all_vehicle_trajectory_sha256": whole_network_truth.hexdigest(),
        "min_gap_violating_adjacent_pair_ticks": gap_violation_ticks,
        "geometrically_overlapping_adjacent_pair_ticks": overlap_ticks,
        "mean_lane_density_vehicles_per_km": {
            lane: ticks / count * 1000 / cfg["geometry"]["length_m"]
            for lane, ticks in sorted(lane_population_exposure.items())
        },
        "target_background_longitudinal_distance_histogram": {
            "bin_upper_edges_exclusive_m": distance_bin_upper_m,
            "last_bin": "5000m and above",
            "weights": "one sample per target-background vehicle-tick",
            "by_target": distance_hist,
        },
    }
    try:
        snapshot = {**cfg, "scenario": job["scenario"]}
        if not events and job.get("stage") == "resource_only":
            motion_facts = {}
        else:
            plans, primary = event_plans(inputs, snapshot, clock, set())
            motion_facts = truth_and_physics(destination, plans, primary, snapshot, clock, set())
        geometry_report["physics"] = {
            name: {**v, **{k: clock.seconds(v[k]) for k in ("lower", "upper", "stop")}}
            for name, v in motion_facts.items()
        }
        failures = set(anomalies) - {"minGap_violation_examples"}
        if failures:
            raise ValueError(f"Independent SUMO anomalies: {sorted(failures)}")
        geometry_report["valid"] = True
    except ValueError as exc:
        geometry_report.update(valid=False, reason=str(exc))
        raise
    finally:
        write_json(destination / "validation.json", geometry_report)
    return geometry_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--scenario", required=True, choices=("S0", "S1", "S2", "E0"))
    parser.add_argument("--density", type=float)
    parser.add_argument("--resource-count", type=int)
    parser.add_argument("--engineering-count", type=int)
    parser.add_argument("--resource-seconds", type=float, default=4)
    parser.add_argument("--seed", type=int, default=200)
    parser.add_argument("--sumo-only", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    job = {"scenario": args.scenario, "seed": args.seed, "density_per_km_lane": args.density}
    if args.engineering_count is not None:
        config["engineering_geometry"] = {"background_vehicles": args.engineering_count}
    if args.resource_count is not None:
        job.update(
            stage="resource_only",
            background_count=args.resource_count,
            resource_duration_s=args.resource_seconds,
        )
    try:
        result = prepare_inputs(config, job, args.directory / "inputs")
        if args.sumo_only:
            result = audit_sumo(config, job, args.directory / "inputs", args.directory / "sumo_only")
    except Exception as exc:
        fail = {"valid": False, "error": str(exc), "type": type(exc).__name__}
        if args.directory.exists() and not (args.directory / "failure.json").exists():
            write_json(args.directory / "failure.json", fail)
        print(json.dumps(fail, ensure_ascii=True), flush=True)
        return 1
    print(
        json.dumps(
            {
                k: result.get(k)
                for k in (
                    "valid",
                    "scenario",
                    "seed",
                    "background_prefill_n",
                    "actual_insertions",
                    "normal_exits",
                    "peak_all_vehicles",
                    "physics",
                )
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
