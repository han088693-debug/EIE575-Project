"""Actual per-attempt OMNeT++ / Veins 5 / SUMO engine for the formal CLI."""

import csv
from contextlib import ExitStack
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import psutil

PROJECT = Path(__file__).resolve().parents[1]
SAMPLE_S = 0.25


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def put_json(path, data):
    from experiment import save

    for trial in range(8):
        try:
            save(path, data)
            return
        except PermissionError as exc:
            if getattr(exc,"winerror",None) not in (5,32,33) or trial==7:
                raise
            time.sleep(0.05*(trial+1))


def mode(config):
    value = config.get(
        "observation_mode", config.get("resource_probe", {}).get("logging_mode", "registered_pairs")
    )
    if value not in ("registered_pairs", "full"):
        raise ValueError("Unsupported observation_mode")
    return value


def actual_duration(config):
    return float(config.get("resource_probe", {}).get("duration_s", config["timing"]["duration_s"]))


def effective_ini(cfg, job):
    c = cfg["control"]
    t = cfg["timing"]
    w = cfg["wireless"]
    m = cfg["motion"]
    g = cfg["geometry"]
    speed = m["speed_kmh"] / 3.6
    resolution = t["time_resolution_s"]
    precision = {1e-12: "ps", 1e-9: "ns", 1e-6: "us", 1e-3: "ms"}
    if resolution not in precision:
        raise ValueError("Unsupported OMNeT++ SimTime precision")
    general = {
        "sim-time-limit": f"{actual_duration(cfg):.12g}s",

        "num-rngs": 2,
        "seed-set": job["seed"],
        "*.manager.seed": job["seed"],
        "*.playgroundSizeX": f"{g['length_m']+10}m",
        "*.playgroundSizeY": "1000m",
        "*.playgroundSizeZ": "50m",
        "*.manager.margin": 0,
        "*.node[*].veinsmobility.hostPositionOffset": "0m",
        "*.manager.updateInterval": f"{t['motion_step_s']}s",
        "*.manager.launchConfig": 'xmldoc("traffic.launchd.xml")',
        "*.connectionManager.maxInterfDist": f"{w['interference_distance_m']}m",
        "*.**.nic.mac1609_4.txPower": f"{w['power_mw']}mW",
        "*.**.nic.mac1609_4.bitrate": f"{w['bitrate_bps']}bps",
        "*.**.nic.phy80211p.minPowerLevel": f"{w['min_power_dbm']}dBm",
        "*.**.nic.phy80211p.noiseFloor": f"{w['noise_floor_dbm']}dBm",
        "*.**.nic.mac1609_4.rng-0": 1,
    }
    app = {
        "formalMode": "true",
        "formalScenario": json.dumps(job["scenario"]),
        "formalSeed": job["seed"],
        "formalWarmup": f"{t['warmup_s']}s",
        "formalGrid": f"{t['truth_step_s']}s",
        "formalFullLog": "true" if mode(cfg) == "full" else "false",
        "scheme": json.dumps(job["scheme"]),
        "beaconUserPriority": w["priority"],
        "messageBits": f"{w['message_bits']}bit",
        "controlPeriod": f"{t['control_step_s']}s",
        "lookback": f"{c['lookback_s']}s",
        "eventValidity": f"{t['event_validity_s']}s",
        "brakeThreshold": f"{m['brake_threshold_mps2']}mpss",
        "errorScale": f"{c['error_scale_m']}m",
        "speedReference": f"{c['g1_reference_kmh']/3.6:.16g}mps",
        "rMinHz": c["r_min_hz"],
        "rMaxHz": c["r_max_hz"],
        "initialBaseHz": c["b_initial_hz"],
        "beta": c["beta"],
        "cbrTarget": c["cbr_target"],
        "cbrDeadband": c["deadband"],
        "feedbackGain": c["feedback_gain"],
        "smoothingOld": c["smoothing_old"],
        "jitterFraction": c["jitter_fraction"],
        "backgroundRateHz": c.get("background_rate_hz", 0),
    }
    brake = {
        "formalMode": "true",
        "formalScenario": json.dumps(job["scenario"]),
        "formalWarmup": f"{t['warmup_s']}s",
        "formalLocalRadius": f"{g['local_radius_m']}m",
        "brakeTime": f"{t['warmup_s']}s",
        "brakeDuration": f"{speed/m['deceleration_mps2']:.16g}s",
        "targetSpeed": "0mps",
        "updateInterval": f"{t['truth_step_s']}s",
        "deadline": f"{t['deadline_s']}s",
        "observationWindow": f"{t['observation_s']}s",
    }
    for prefix, values in (("*.node[*].appl.", app), ("*.brakeScenario.", brake)):
        general.update({prefix + k: v for k, v in values.items()})
    return (
        "include baseline.ini\n\n[General]\nsimtime-resolution = "+precision[resolution]+"\n\n[Config Formal]\n"
        + "\n".join(f"{k} = {v}" for k, v in general.items())
        + "\n"
    )


def prepare_inputs(config, job, destination):
    from formal_scenario import prepare_inputs as prepare_scenario

    dest = Path(destination).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    mapped = dict(job)
    if config.get("density_per_lane_km") is not None:
        mapped["density_per_km_lane"] = config["density_per_lane_km"]
    prepare_scenario(config, mapped, dest)
    for name in (
        "traffic.launchd.xml",
        "traffic.sumo.cfg",
        "traffic.rou.xml",
        "highway.net.xml",
        "pairs.csv",
        "prior.csv",
        "events.csv",
    ):
        if not (dest / name).is_file():
            raise FileNotFoundError(f"Scenario generator omitted {name}")
    shutil.copyfile(PROJECT / "omnetpp.ini", dest / "baseline.ini")
    shutil.copyfile(PROJECT / "radio.xml", dest / "radio.xml")
    (dest / "formal.ini").write_text(effective_ini(config, job), encoding="utf-8")


def owned_api(settings):
    path = Path(settings["mcp_dir"]) / "process_control.py"
    spec = spec_from_file_location("tabr_owned_process", path)
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.OwnedProcess


def runtime(settings):
    from manage import tool_paths

    omnet, veins, sumo = (Path(settings[k]).resolve() for k in ("omnet_root", "veins_root", "sumo_bin"))
    _, _, _, _, env = tool_paths(settings["root"])
    mingw = omnet / "tools/win64/mingw64/bin"
    env["PATH"] = os.pathsep.join(str(x) for x in (omnet / "bin", mingw, veins / "src", sumo, env["PATH"]))
    env["QT_PLUGIN_PATH"] = str(omnet / "tools/win64/mingw64/share/qt5/plugins")
    return veins, sumo, env


def process_commands(cfg, job, attempt, port):
    settings = cfg["tools"]
    veins, sumo, env = runtime(settings)
    logs = attempt / "logs"
    launch = [
        settings["launcher_python"],
        "-u",
        str(veins / "sumo-launchd.py"),
        "-c",
        str(sumo / "sumo.exe"),
        "-b",
        "127.0.0.1",
        "-p",
        str(port),
        "-L",
        str(logs / "launchd.log"),
        "-vv",
        "-t",
    ]
    sim = [
        str(PROJECT / "EIE575_TABR_Starter.exe"),
        "-u",
        "Cmdenv",
        "-f",
        "formal.ini",
        "-c",
        "Formal",
        "-n",
        str(PROJECT / "src") + ";" + str(veins / "src/veins"),
        f"--*.manager.port={port}",
        "--result-dir=" + logs.as_posix(),
    ]
    run_id = read_json(attempt / "run_manifest.json")["run_id"]
    for prefix in ("*.node[*].appl", "*.brakeScenario"):
        sim += [
            "--" + prefix + ".runId=" + json.dumps(run_id),
            "--" + prefix + '.outputDir="' + logs.as_posix() + '"',
        ]
    return launch, sim, env


def lookup_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class CounterReader:
    def __init__(self, log):
        self.path = log
        self.offset = 0
        self.fields = None
        self.last = {}

    def sample(self):
        if not self.path.is_file():
            return self.last
        with self.path.open("rb") as f:
            f.seek(self.offset)
            while True:
                row = f.readline()
                if not row.endswith(b"\n"):
                    break
                self.offset = f.tell()
                fields = next(csv.reader([row.decode("utf-8-sig").strip()]))
                if self.fields is None:
                    self.fields = fields
                elif len(fields) == len(self.fields):
                    self.last = dict(zip(self.fields, fields))
        return self.last


class Monitor:
    def __init__(self, attempt, stream, started):
        self.stream = stream
        self.writer = csv.DictWriter(
            stream,
            fieldnames="t_wall_s private_bytes handles active_vehicle_count open_log_streams process_count rss_bytes cpu_seconds t_sim_s peak_open_log_streams".split(),
        )
        self.writer.writeheader()
        self.started = started
        self.counters = CounterReader(attempt / "logs/engine_counters.csv")
        self.pids = {}
        self.peak_private = 0
        self.peak_handles = 0

    def sample(self, processes):
        observed = {}
        for proc in processes:
            try:
                root = psutil.Process(proc.process.pid)
                if root.create_time() != proc.identity["create_time"]:
                    continue
                for child in [root, *root.children(recursive=True)]:
                    observed[child.pid] = child
            except psutil.NoSuchProcess:
                continue
        private = rss = handles = 0
        cpu = 0.0
        for pid, proc in observed.items():
            try:
                with proc.oneshot():
                    when = proc.create_time()
                    self.pids[str(pid) + ":" + str(when)] = {
                        "pid": pid,
                        "create_time": when,
                        "name": proc.name(),
                    }
                    mem = proc.memory_info()
                    private += mem.private
                    rss += mem.rss
                    handles += proc.num_handles()
                    times = proc.cpu_times()
                    cpu += times.user + times.system
            except psutil.NoSuchProcess:
                continue
        self.peak_private = max(private, self.peak_private)
        self.peak_handles = max(handles, self.peak_handles)
        counters = self.counters.sample()
        self.writer.writerow(
            dict(
                t_wall_s=round(time.monotonic() - self.started, 6),
                private_bytes=private,
                rss_bytes=rss,
                handles=handles,
                process_count=len(observed),
                cpu_seconds=cpu,
                **{
                    k: counters.get(k, "")
                    for k in ("t_sim_s", "active_vehicle_count", "open_log_streams", "peak_open_log_streams")
                },
            )
        )
        self.stream.flush()
        return cpu


def traffic_diagnostics(attempt, raw, duration):
    """Reconcile scheduled IDs, SUMO-inserted vehicles, completed trips and alerts."""
    route = ET.parse(attempt / "inputs/traffic.rou.xml").getroot()
    if route.findall("flow") or route.findall("trip"):
        raise ValueError("Entry accounting requires individually registered vehicles")
    planned = {
        v.attrib["id"] for v in route.findall("vehicle") if float(v.attrib["depart"]) < duration - 1e-9
    }
    last = None
    for _, node in ET.iterparse(raw / "summary.xml", events=("end",)):
        if node.tag == "step":
            last = dict(node.attrib)
        node.clear()
    if last is None:
        raise ValueError("SUMO summary has no steps")
    inserted = int(last["inserted"])
    entered = set()
    arrived = set()
    abnormal = {}
    for _, node in ET.iterparse(raw / "trips.xml", events=("end",)):
        if node.tag == "tripinfo":
            a = node.attrib
            vid = a["id"]
            if float(a["depart"]) >= 0:
                if vid in entered:
                    raise ValueError("Duplicate SUMO trip: " + vid)
                entered.add(vid)
                if float(a["arrival"]) >= 0:
                    arrived.add(vid)
                if a.get("vaporized", "").lower() not in ("", "false", "0", "unfinished"):
                    abnormal[vid] = a["vaporized"]
            node.clear()
    if inserted != len(entered):
        raise ValueError(f"SUMO summary inserted {inserted} vs {len(entered)} entered trips")
    warning = (raw / "sumo-launchd.err.log").read_text(encoding="utf-8", errors="replace").splitlines()
    teleport = [x for x in warning if re.search(r"(?:teleporting vehicle|starting teleport)", x, re.I)]
    collisions = [x for x in warning if re.search(r"\bcollision(?: with| between| of|\b.*vehicle)", x, re.I)]
    gap = [float(v) for line in collisions for v in re.findall(r"(?<!min)\bgap=([-+\d.]+)", line, re.I)]
    early_leave = len(arrived ^ {v for v in arrived if v in planned})
    anomaly = abs(int(last["ended"]) - len(arrived)) + len(abnormal) + early_leave
    return dict(
        expected_entries=len(planned),
        actual_entries=inserted,
        insertion_failures=len(planned - entered),
        unexpected_entries=sorted(entered - planned),
        missing_entries=sorted(planned - entered),
        unexplained_teleports=len(teleport),
        unexplained_removals=anomaly,
        normal_arrivals=len(arrived - set(abnormal)),
        running_at_end=int(last["running"]),
        summary_last=last,
        abnormal_trips=abnormal,
        teleport_warnings=teleport,
        collision_warnings=collisions,
        sumo_collision_warning_count=len(collisions),
        negative_gap_warnings=sum(v < 0 for v in gap),
        physical_collision_count=None,
        min_gap_note="SUMO threshold warnings are not an independent full-fleet bumper-gap measurement.",
        raw_evidence_paths={
            n: str((raw / n).relative_to(attempt))
            for n in ("summary.xml", "trips.xml", "sumo-launchd.err.log", "traffic.sumo.cfg")
        },
    )


def read_exit(log):
    try:
        contents = log.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    code = re.search(r"<exit-code>(-?\d+)</exit-code>", contents)
    return int(code.group(1)) if code else None


def run_engine(config, job, attempt):
    from experiment import Failure, file_hash
    from wall_budget import Supervisor

    attempt = Path(attempt).resolve()
    inputs = attempt / "inputs"
    logs = attempt / "logs"
    generated_log = inputs / ".cmdenv-log"
    if generated_log.exists():
        raise ValueError("Unexpected generated Cmdenv log before start")
    scratch = attempt / "veins_runtime"
    scratch.mkdir()
    port = lookup_port()
    launch, sim, env = process_commands(config, job, attempt, port)
    env.update(TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch))
    cap = config.get("execution", {}).get("wall_timeout_s", 1800)
    if not isinstance(cap, (int, float)) or not 0 < cap:
        raise ValueError("Positive per-job wall timeout required")
    status = dict(
        engine_complete=False,
        status="starting",
        return_code=None,
        pids=[],
        port=port,
        commands=[launch, sim],
        cwd=str(inputs),
        wall_timeout_s=cap,
        sample_period_s=SAMPLE_S,
        observation_mode=mode(config),
    )
    state_file = logs / "engine_status.json"
    started = time.monotonic()
    budget = Supervisor(PROJECT, attempt, cap, os.environ.get("EIE575_WALL_REQUEST"),
                        os.environ.get("EIE575_WALL_REGISTRATION"), started=started)
    status.update(wall_timeout_s=budget.limit, authorized_wall_timeout_s=budget.cap,
                  budget_supervisor_sha256=file_hash(Path(__file__).with_name("wall_budget.py")))
    put_json(state_file, status)
    monitor = None
    try:
        owner = owned_api(config["tools"])
        with ExitStack() as stack:
            resource_file = stack.enter_context(
                (logs / "resource_trace.csv").open("w", encoding="utf-8", newline="")
            )
            monitor = Monitor(attempt, resource_file, started)
            procs = []

            def spawn(command, log):
                output = stack.enter_context((logs / log).open("wb"))
                proc = stack.enter_context(
                    owner(
                        [sys.executable, "-B", str(Path(__file__).resolve()), "--owned-child", *command],
                        inputs,
                        env,
                        output,
                    )
                )
                procs.append(proc)
                return proc

            launcher = spawn(launch, "launcher_console.log")
            # The listener's owner is checked so an unrelated process cannot supply the chosen free port.
            deadline = min(started + budget.limit, started + 20)
            while True:
                monitor.sample(procs)
                if launcher.poll() is not None:
                    raise RuntimeError("Veins launcher exited before listening")
                if any(
                    pid["name"].lower() in ("python2.exe", "python.exe") and owns_listener(pid, port)
                    for pid in monitor.pids.values()
                ):
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("Veins launcher listener timed out")
                time.sleep(0.08)
            solver = spawn(sim, "simulation.log")
            status.update(status="running", pids=list(monitor.pids.values()))
            put_json(state_file, status)
            while solver.poll() is None:
                cpu = monitor.sample(procs)
                if launcher.poll() is not None:
                    raise RuntimeError("Veins launcher exited during simulation")
                budget.check(cpu)
                if status["wall_timeout_s"] != budget.limit:
                    status["wall_timeout_s"] = budget.limit
                    put_json(state_file, status)
                time.sleep(min(SAMPLE_S, max(0, started + budget.limit - time.monotonic())))
            if time.monotonic() - started >= budget.cap:
                budget.check(0)
            code = solver.poll()
            status["return_code"] = code
            if code:
                raise RuntimeError(f"Cmdenv exited {code}; see logs/simulation.log")
            deadline = min(started + budget.limit, time.monotonic() + 20)
            while (sumo_exit := read_exit(logs / "launchd.log")) is None:
                monitor.sample(procs)
                if time.monotonic() >= deadline:
                    raise RuntimeError("Missing launchd SUMO exit evidence")
                time.sleep(0.12)
            if sumo_exit != 0:
                raise RuntimeError(f"SUMO exited {sumo_exit}; see logs/launchd.log")
            monitor.sample(procs)
        raw_dirs = list(scratch.glob("sumo-launchd-tmp-*"))
        if len(raw_dirs) != 1:
            raise ValueError("Expected exactly one owned SUMO workspace")
        try:
            diag = traffic_diagnostics(attempt, raw_dirs[0], actual_duration(config))
        except (OSError, ET.ParseError, KeyError, ValueError) as exc:
            raise Failure(30, "Invalid SUMO diagnostics: " + str(exc)) from exc
        put_json(logs / "sumo_diagnostics.json", diag)
        if (
            diag["expected_entries"] != diag["actual_entries"]
            or diag["missing_entries"]
            or diag["unexpected_entries"]
            or diag["unexplained_removals"]
            or diag["unexplained_teleports"]
        ):
            raise Failure(30, "Traffic accounting failed; see logs/sumo_diagnostics.json")
        status.update(engine_complete=True, status="completed", exit_code=0)
    except BaseException as exc:
        status.update(
            status="interrupted_timeout" if isinstance(exc, TimeoutError) else "failed", error=str(exc)
        )
        raise
    finally:
        status["wall_seconds"] = time.monotonic() - started
        status["wall_timeout_s"] = budget.limit
        if monitor is not None:
            status.update(
                pids=list(monitor.pids.values()),
                peak_private_bytes=monitor.peak_private,
                peak_memory_bytes=monitor.peak_private,
                peak_handles=monitor.peak_handles,
            )
        original_error = sys.exc_info()[1]
        try:
            put_json(logs / "engine_terminal.json", status)
        except OSError as terminal_error:
            # The independent engine status can still preserve the primary outcome.
            status["terminal_snapshot_error"] = str(terminal_error)
        try:
            try:
                put_json(state_file, status)
            except OSError as early_error:
                status["early_status_error"] = str(early_error)
            budget.close()
            status["cmdenv_log"] = archive_cmdenv(generated_log, logs / "cmdenv-internal.log")
        except (OSError, ValueError) as cleanup_error:
            status["finalization_error"] = str(cleanup_error)
            if original_error is None:
                status.update(status="evidence_pending", engine_complete=False)
                raise
        finally:
            try:
                put_json(state_file, status)
            except OSError:
                if original_error is None:
                    raise
    return status


def archive_cmdenv(source, destination):
    from experiment import file_hash

    if not source.is_file():
        return {"state": "not_generated"}
    try:
        source.replace(destination)
        return {"state": "moved"}
    except PermissionError:
        # A lingering Windows reader can allow reading while denying rename.
        # Keep both copies and bind their bytes. Both also enter the attempt inventory.
        shutil.copyfile(source, destination)
        digest = file_hash(destination)
        if digest != file_hash(source):
            raise ValueError("Cmdenv log changed during finalization")
        return {"state": "copied_source_retained", "sha256": digest}


def owns_listener(identity, port):
    try:
        proc = psutil.Process(identity["pid"])
        if proc.create_time() != identity["create_time"]:
            return False
        return any(
            c.status == psutil.CONN_LISTEN and c.laddr.port == port for c in proc.connections(kind="tcp4")
        )
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


if __name__ == "__main__":
    # Required before starting the actual command: the supervisor first attaches the shim to its Job Object.
    if len(sys.argv) < 3 or sys.argv[1] != "--owned-child" or sys.stdin.buffer.readline() != b"GO\n":
        sys.exit(2)
    sys.exit(subprocess.call(sys.argv[2:], stdin=subprocess.DEVNULL, stdout=sys.stdout.buffer, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW))
