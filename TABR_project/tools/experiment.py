"""Serial, resumable TABR experiments. Run with --help for command contracts."""

import argparse
import csv
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timezone

PROJECT = Path(__file__).resolve().parents[1]
SCHEMES = ("G0", "G1", "G2", "G3")
SCENARIO_LOADS = (("S1", ("low", "high")), ("S0", ("low", "high")), ("S2", ("high",)))
CODES = {
    "running": 40,
    "interrupted": 40,
    "environment_error": 10,
    "engine_error": 20,
    "invalid": 30,
    "completed": 0,
}


class Failure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def identity(value):
    return digest(
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def file_hash(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def json_read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def save(path, value):
    write_atomic(path, json.dumps(value, allow_nan=False, indent=2, ensure_ascii=False).encode("utf-8"))


@contextmanager
def locked(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".batch.lock").open("a+b") as stream:
        if not stream.seek(0, 2):
            stream.write(b"x")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise Failure(40, "Batch is in use: " + str(directory)) from exc
        yield


def read_config(path):
    from experiment_config import load_config

    return load_config(path)


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise Failure(2, "Invalid identifier: " + str(value))
    return value


def matrices(config):
    if list(config["seeds"]["formal"]) != list(range(5)):
        raise Failure(2, "Core formal seeds must be [0,1,2,3,4]")
    jobs = []
    for scenario, loads in SCENARIO_LOADS:
        for load in loads:
            for seed in config["seeds"]["formal"]:
                for scheme in SCHEMES:
                    block = "%s_%s_seed%d" % (scenario, load, seed)
                    jobs.append(
                        dict(
                            job_id="%s_%s_%s_seed%d" % (scenario, load, scheme, seed),
                            scenario=scenario,
                            load_id=load,
                            seed=seed,
                            scheme=scheme,
                            block_id=block,
                        )
                    )
    return jobs


# Scientific configuration is portable across alternate installation/checkpoint paths.
# Use an explicit list of administrative fields, keep all other scientific inputs.
def physics_fields(cfg):
    excluded = {
        "stage",
        "p3_evidence",
        "paths",
        "tool_paths",
        "tools",
        "output_root",
        "experiment_id",
        "analysis",
        "analysis_version",
        "analysis_rules",
        "metrics",
        "reporting",
        "calibration_evidence",
        "analysis_interpreter",
        "scope",
        "execution",
    }
    return {key: value for key, value in cfg.items() if key not in excluded}


def freeze_physics_sha256(cfg):
    return identity(physics_fields(cfg))


def check_gate(cfg, origin):
    if cfg.get("engineering_geometry") or cfg.get("resource_probe"):
        raise Failure(2, "Engineering overrides cannot enter a formal batch")
    if cfg.get("stage") != "frozen":
        raise Failure(2, "Formal simulation requires a frozen configuration")
    receipt = cfg.get("p3_evidence") or {}
    try:
        path = origin.parent / receipt["path"]
        if file_hash(path) != receipt["sha256"].lower():
            raise ValueError("P3 evidence SHA-256 mismatch")
        record = json_read(path)
        if (
            record.get("phase") != "P3"
            or record.get("passed") is not True
            or record.get("freeze_physics_sha256") != freeze_physics_sha256(cfg)
        ):
            raise ValueError("P3 evidence does not certify this physical configuration")

        from p3_execution import verify_policy
        verify_policy(cfg, record, PROJECT)
    except (OSError, KeyError, ValueError, TypeError) as exc:
        raise Failure(2, "Frozen P3 evidence failed: " + str(exc)) from exc
    return path


def hashes(root):
    if not root.is_dir():
        return {}
    return {f.relative_to(root).as_posix(): file_hash(f) for f in sorted(root.rglob("*")) if f.is_file()}


def simulation_sources(project):
    files = [
        project / name
        for name in (
            "omnetpp.ini",
            "radio.xml",
            "EIE575_TABR_Starter.exe",
            "tools/formal_engine.py",
            "tools/wall_budget.py",
            "tools/timeout_recovery.py",
            "tools/manage.py",
        )
    ]
    for pattern in (
        "src/*.cc",
        "src/*.h",
        "src/*.msg",
        "src/*.ned",
        "scenarios/*.xml",
        "scenarios/*.cfg",
        "tools/*scenario*.py",
        "tools/*engine*.py",
    ):
        files.extend(project.glob(pattern))
    return {
        path.relative_to(project).as_posix(): file_hash(path) for path in sorted(set(files)) if path.is_file()
    }


def runtime_hashes(cfg):
    settings = cfg.get("tools", {})
    result = {}
    binaries = {
        "veins_root": "src/libveins.dll",
        "sumo_bin": "sumo.exe",
        "omnet_root": "bin/liboppsim.dll",
        "launcher_python": "",
    }
    for key, relative in binaries.items():
        if key in settings:
            path = Path(settings[key]) / relative
            result[key] = file_hash(path)
    if "sumo_bin" in settings:
        result["netconvert"] = file_hash(Path(settings["sumo_bin"]) / "netconvert.exe")
    return result


def analysis_id(project, cfg):
    patterns = ["formal_analy*.py", "formal_metric*.py", "formal_events.py", "formal_state.py", "terminal_coverage.py"]
    if cfg.get("resource_probe"):
        patterns.append("resource_probe.py")
    modules = sorted(
        {
            p
            for pattern in patterns
            for p in (project / "tools").glob(pattern)
        }
    )
    return identity(
        dict(
            code={p.name: file_hash(p) for p in modules},
            rules={
                key: cfg[key]
                for key in ("analysis", "analysis_version", "analysis_rules", "metrics", "reporting")
                if key in cfg
            },
        )
    )


def input_hashes(folder):
    files = hashes(folder)
    if ".cmdenv-log" in files:
        archived = folder.parent / "logs/cmdenv-internal.log"
        if not archived.is_file() or file_hash(archived) != files[".cmdenv-log"]:
            raise ValueError("Unexpected or unverified Cmdenv output in input directory")
        del files[".cmdenv-log"]
    return files


def input_fingerprint(folder, generation_dir=None):
    files = input_hashes(folder)
    if not files:
        raise ValueError("The engine did not prepare any input files")
    original = Path(generation_dir) if generation_dir else folder.resolve()
    resolved = original.as_posix()

    def portable_value(value):
        # Relative path is the stable, physically meaningful part of local references.
        normalized = value.replace("\\", "/")
        for root, label in (
            (resolved, "@inputs"),
            (original.parent.as_posix(), "@attempt"),
        ):
            normalized = re.sub(re.escape(root) + r"(?=/|$)", lambda match: label, normalized, flags=re.I)
        return normalized

    values = {}
    for name, byte_hash in files.items():
        p = folder / name
        if p.suffix.lower() in (".xml", ".sumocfg", ".cfg"):
            root = ET.parse(p).getroot()
            for node in root.iter():
                for key, val in list(node.attrib.items()):
                    node.set(key, portable_value(val))
            values[name] = digest(ET.canonicalize(ET.tostring(root, encoding="unicode")).encode("utf-8"))
        elif p.suffix.lower() == ".ini":
            rows = []
            for row in p.read_text(encoding="utf-8-sig").splitlines():
                key, sep, val = row.partition("=")
                if sep and (
                    key.strip() in ("result-dir", "*.manager.port") or key.strip().endswith(".runId")
                ):
                    continue
                rows.append(key + sep + portable_value(val) if sep else row)
            values[name] = digest("\n".join(rows).encode("utf-8"))
        else:
            values[name] = byte_hash
    return identity(values)


def matrix_bytes(jobs):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(jobs[0]))
    writer.writeheader()
    writer.writerows(jobs)
    return stream.getvalue().encode("utf-8")


def new_plan(cfg, scope, project):
    exp = safe_id(cfg["experiment_id"])
    jobs = matrices(cfg)
    source = simulation_sources(project)
    source.update({"tool:" + k: v for k, v in runtime_hashes(cfg).items()})
    return dict(
        format_version=1,
        experiment_id=exp,
        scope=scope,
        config=cfg,
        sources=source,
        physics_sha256=freeze_physics_sha256(cfg),
        formal_jobs=len(jobs) if scope == "formal" else 0,
        paired_blocks=len({j["block_id"] for j in jobs}),
        duplicate_jobs=0,
        jobs=jobs,
        attempts={j["job_id"]: [] for j in jobs},
        created_at=now(),
    )


def plan(args, project, load):
    cfg = load(args.config)
    receipt = check_gate(cfg, args.config.resolve()) if args.scope == "formal" and not args.dry_run else None
    b = new_plan(cfg, args.scope, project)
    output = (args.output or (project / "results" / "batches" / b["experiment_id"])).resolve()
    if args.dry_run:
        return (
            dict(
                dry_run=True,
                output=str(output),
                **{
                    k: b[k]
                    for k in (
                        "scope",
                        "formal_jobs",
                        "paired_blocks",
                        "duplicate_jobs",
                        "jobs",
                        "config",
                        "physics_sha256",
                    )
                }
            ),
            0,
        )
    with locked(output):
        if (output / "batch.json").exists():
            raise Failure(2, "Batch already exists; choose a new output directory")
        if receipt is not None:
            write_atomic(output / "p3_evidence.json", receipt.read_bytes())
        b["config_origin"] = str(args.config.resolve())
        b["matrix_sha256"] = digest(matrix_bytes(b["jobs"]))
        write_atomic(output / "matrix.csv", matrix_bytes(b["jobs"]))
        save(output / "batch.json", b)
    return (
        dict(
            batch=str(output),
            formal_jobs=b["formal_jobs"],
            paired_blocks=b["paired_blocks"],
            duplicate_jobs=0,
        ),
        0,
    )


def batch_read(path):
    b = json_read(path / "batch.json")
    if (
        b.get("format_version") != 1
        or freeze_physics_sha256(b["config"]) != b["physics_sha256"]
        or file_hash(path / "matrix.csv") != b["matrix_sha256"]
        or digest(matrix_bytes(b["jobs"])) != b["matrix_sha256"]
        or (b["scope"] != "engineering" and b["jobs"] != matrices(b["config"]))
    ):
        raise Failure(30, "Batch matrix failed verification")
    return b


def attempt_dir(batch, b, job, token):
    safe_id(token)
    result_root = batch.parent.parent if batch.parent.name == "batches" else batch / "results"
    if b["scope"] != "formal":
        result_root = result_root / b["scope"]
    return (
        result_root
        / safe_id(b["experiment_id"])
        / job["scenario"]
        / job["load_id"]
        / job["scheme"]
        / ("seed" + str(job["seed"]))
        / token
    )


def job_snapshot(config, job, scope):
    density = config.get("density_per_lane_km")
    if density is None:
        density = config.get("loads", {}).get(job["load_id"], {}).get("density_per_km_lane")
    logging_mode = config.get(
        "observation_mode",
        config.get("resource_probe", {}).get(
            "logging_mode", "full" if scope == "synthetic" else "registered_pairs"
        ),
    )
    snapshot = dict(
        config,
        observation_mode=logging_mode,
        scenario=job["scenario"],
        scheme=job["scheme"],
        seed=job["seed"],
        load_id=job["load_id"],
        density_per_lane_km=density,
        data_origin="synthetic" if scope == "synthetic" else "simulation",
    )
    if "execution" in config:
        from p3_execution import effective_policy
        snapshot["execution"] = effective_policy(config, job)
    return snapshot


def inspect_attempt(path, b, job):
    try:
        m = json_read(path / "run_manifest.json")
        if (
            m["attempt_id"] != path.name
            or m["job_id"] != job["job_id"]
            or m["scope"] != b["scope"]
            or any(m.get(k) != job[k] for k in job)
        ):
            return "invalid", None
        if m["status"] != "completed":
            return ("interrupted" if m["status"] == "running" else m["status"]), None
        if not m["data_valid"] or json_read(path / "config_snapshot.json") != job_snapshot(
            b["config"], job, b["scope"]
        ):
            return "invalid", None
        actual = {p: v for p, v in hashes(path).items() if p != "run_manifest.json"}
        if actual != m["files"]:
            return "invalid", None
        expected = job_identity(b, job, input_fingerprint(path / "inputs", m.get("generation_dir")))
        if m["simulation_job_key"] != expected:
            return "invalid", None
        return "completed", m
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError):
        return "invalid", None


def job_identity(b, j, inputs_id):
    return identity(
        dict(
            physics=b["physics_sha256"],
            sources=b["sources"],
            scope=b["scope"],
            job=j,
            effective_inputs=inputs_id,
        )
    )


def selected(batch, b, job):
    prior = "pending"
    for token in b["attempts"][job["job_id"]]:
        path = attempt_dir(batch, b, job, token)
        prior, m = inspect_attempt(path, b, job)
        if m is not None:
            return prior, path, m
    return prior, None, None


def inventory(batch, b):
    counts = {
        key: 0
        for key in (
            "completed",
            "valid",
            "invalid",
            "interrupted",
            "pending",
            "engine_error",
            "environment_error",
        )
    }
    good = {}
    per_job = {}
    for j in b["jobs"]:
        state, path, m = selected(batch, b, j)
        counts[state] = counts.get(state, 0) + 1
        per_job[j["job_id"]] = state
        if m:
            counts["valid"] += 1
            good[j["job_id"]] = (path, m)
    return counts, good, per_job


def synthetic_engine(project):
    from importlib.util import spec_from_file_location, module_from_spec
    from types import SimpleNamespace

    path = project / "tests" / "formal_fixtures.py"
    if not path.is_file():
        raise Failure(10, "Synthetic fixture engine missing: " + str(path))
    spec = spec_from_file_location("formal_fixtures", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return SimpleNamespace(prepare_inputs=module.prepare_fixture_inputs, run_engine=module.run_engine)


def require_components(engine, analyzer):
    try:
        engine = engine or importlib.import_module("formal_engine")
        analyzer = analyzer or importlib.import_module("formal_analyze").analyze_attempt
        if not callable(engine.prepare_inputs) or not callable(engine.run_engine) or not callable(analyzer):
            raise AttributeError("Missing engine/analysis interface")
    except (ImportError, AttributeError) as exc:
        raise Failure(10, "Formal engine/analyzer unavailable: " + str(exc)) from exc
    return engine, analyzer


def run_one(batch, b, j, config, engine, analyzer, project):
    token = "attempt-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:12]
    p = attempt_dir(batch, b, j, token)
    p.mkdir(parents=True)
    for name in ("inputs", "logs"):
        (p / name).mkdir()
    config = job_snapshot(config, j, b["scope"])
    save(p / "config_snapshot.json", config)
    m = dict(
        **j,
        attempt_id=token,
        run_id=token,
        scope=b["scope"],
        status="running",
        data_valid=False,
        observation_mode=config["observation_mode"],
        data_origin=config["data_origin"],
        started_at=now(),
        controller_pid=os.getpid(),
        physics_sha256=b["physics_sha256"],
        sources=b["sources"]
    )
    b["attempts"][j["job_id"]].append(token)
    save(p / "run_manifest.json", m)
    save(batch / "batch.json", b)
    start = time.monotonic()
    try:
        engine.prepare_inputs(config, dict(j), p / "inputs")
        input_raw = hashes(p / "inputs")
        m["generation_dir"] = str((p / "inputs").resolve())
        m["simulation_job_key"] = job_identity(b, j, input_fingerprint(p / "inputs"))
        save(p / "run_manifest.json", m)
        engine_details = engine.run_engine(config, dict(j), p)
        m["execution"] = engine_details or {}
        if isinstance(engine_details, dict):
            if engine_details.get("exit_code", 0) != 0:
                raise Failure(20, "Engine returned nonzero exit code: " + str(engine_details["exit_code"]))
            for key in ("observation_mode", "peak_memory_bytes", "commands", "pids", "trajectory_sha256"):
                if key in engine_details:
                    m[key] = engine_details[key]
        if input_hashes(p / "inputs") != input_raw:
            raise Failure(30, "Prepared simulation input files changed during execution")
        save(p / "run_manifest.json", m)
        try:
            metrics = analyzer(p)
            if not isinstance(metrics, dict) or metrics.get("data_valid") is False:
                raise ValueError("Analyzer returned an invalid result")
            if not any(entry.is_file() for entry in (p / "logs").rglob("*")):
                raise ValueError("No simulation logs")
            save(p / "metrics.json", metrics)
        except (ValueError, OSError, KeyError) as exc:
            raise Failure(30, str(exc)) from exc
        m.update(
            status="completed",
            data_valid=True,
            analysis_key=identity([m["simulation_job_key"], analysis_id(project, config)]),
        )
    except (KeyboardInterrupt, SystemExit):
        m.update(status="interrupted", error="Execution interrupted")
        raise
    except Exception as exc:
        code = (
            exc.code
            if isinstance(exc, Failure)
            else (10 if isinstance(exc, (ImportError, FileNotFoundError, NotImplementedError)) else 20)
        )
        m.update(
            status={10: "environment_error", 20: "engine_error", 30: "invalid"}.get(code, "engine_error"),
            error=str(exc),
        )
    finally:
        m["completed_at"] = now()
        m["wall_seconds"] = time.monotonic() - start
        m["files"] = {k: v for k, v in hashes(p).items() if k != "run_manifest.json"}
        m["output_bytes"] = sum((p / name).stat().st_size for name in m["files"])
        save(p / "run_manifest.json", m)
    return p, m


def receipt_status(batch, block_id, items):
    key = identity(
        [{k: m[k] for k in ("job_id", "attempt_id", "simulation_job_key", "files")} for m in items]
    )
    receipt = batch / "checkpoints" / (block_id + ".json")
    try:
        r = json_read(receipt)
        ok = (
            isinstance(r.get("library_file_id"), str)
            and r["library_file_id"].startswith("libfile_")
            and bool(re.fullmatch(r"[0-9a-fA-F]{64}", r.get("archive_sha256", "")))
            and r["block_key"] == key
        )
    except (OSError, ValueError, KeyError, TypeError):
        ok = False
    return dict(block_id=block_id, block_key=key, receipt=str(receipt), saved=ok)


def run(args, batch, b, project, engine, analyzer, load):
    cfg = b["config"]
    if b["scope"] == "engineering":
        raise Failure(2, "Engineering probes use a new probe output for each independent run")
    if b["scope"] == "synthetic" and engine is None:
        engine = synthetic_engine(project)
    if args.job and args.job not in b["attempts"]:
        raise Failure(2, "Unknown job ID: " + args.job)
    live = simulation_sources(project)
    live.update({"tool:" + k: v for k, v in runtime_hashes(cfg).items()})
    if freeze_physics_sha256(cfg) != b["physics_sha256"] or live != b["sources"]:
        raise Failure(2, "Simulation inputs changed since planning; use a new batch")
    if Path(b["config_origin"]).exists():
        current = load(Path(b["config_origin"]))
        if freeze_physics_sha256(current) != b["physics_sha256"]:
            raise Failure(2, "Source configuration changed; use a new batch")
    if b["scope"] == "formal":
        # Verify the saved receipt so restoring a batch works on a different host.
        receipt = (cfg.get("p3_evidence") or {}).copy()
        receipt["path"] = "p3_evidence.json"
        check_gate(dict(cfg, p3_evidence=receipt), batch / "config.json")
    engine, analyzer = require_components(engine, analyzer)
    done = []
    target_block = next((j["block_id"] for j in b["jobs"] if j["job_id"] == args.job), None)
    for block_id, block_jobs in blocks(b):
        block = []
        for j in block_jobs:
            state, p, m = selected(batch, b, j)
            if m:
                block.append(m)
                if j["job_id"] == args.job:
                    return dict(job=args.job, status="completed", attempt=str(p), skipped=True), 0
                continue
            if args.job and j["job_id"] != args.job:
                if b["scope"] == "formal" and block_id != target_block:
                    raise Failure(40, "Complete the earlier paired block before selecting this job: " + block_id)
                continue
            if b["attempts"][j["job_id"]] and not args.resume:
                raise Failure(40, "An earlier attempt exists; supply --resume to retain and retry it")
            p, m = run_one(batch, b, j, cfg, engine, analyzer, project)
            done.append(j["job_id"])
            if m["status"] != "completed":
                return (
                    dict(job=j["job_id"], attempt=str(p), status=m["status"], error=m["error"]),
                    CODES[m["status"]],
                )
            block.append(m)
            if args.job and len(block) != len(block_jobs):
                return dict(job=args.job, status="completed", attempt=str(p)), 0
        if len(block) == len(block_jobs) and b["scope"] == "formal":
            receipt = receipt_status(batch, block_jobs[0]["block_id"], block)
            if not receipt["saved"]:
                return dict(status="awaiting_checkpoint", completed_this_call=done, **receipt), (
                    0 if args.job in done else 40
                )
        if args.job in done:
            return dict(job=args.job, status="completed"), 0
    return dict(status="completed", completed_this_call=done), 0


def blocks(b):
    from itertools import groupby

    for block, jobs in groupby(b["jobs"], key=lambda j: j["block_id"]):
        yield block, list(jobs)


def reanalyze(batch, b, project, analyzer):
    counts, good, _ = inventory(batch, b)
    if counts["invalid"]:
        raise Failure(30, "A recorded result has missing/corrupt evidence or invalid data; verify and retry")
    if analyzer is None:
        analyzer = importlib.import_module("formal_analyze").analyze_attempt
    version = analysis_id(project, b["config"])
    dest = batch / "analysis" / version
    outputs = {}
    for job, (p, m) in good.items():
        try:
            result = analyzer(p)
            if not isinstance(result, dict) or result.get("data_valid") is False:
                raise ValueError("Invalid reanalysis")
            target = dest / (job + ".json")
            save(
                target,
                dict(
                    simulation_job_key=m["simulation_job_key"],
                    attempt_id=m["attempt_id"],
                    analysis_key=identity([m["simulation_job_key"], version]),
                    metrics=result,
                ),
            )
            outputs[job] = file_hash(target)
        except (OSError, ValueError, KeyError) as exc:
            raise Failure(30, job + ": " + str(exc)) from exc
    save(dest / "index.json", dict(analysis_version=version, outputs=outputs, planned=len(b["jobs"])))
    return dict(
        analysis_version=version, analyzed=len(outputs), planned=len(b["jobs"]), directory=str(dest)
    ), (0 if len(outputs) == len(b["jobs"]) else 40)


def verify(batch, b, job=None):
    counts, good, per_job = inventory(batch, b)
    receipts = []
    history = {}
    for j in b["jobs"]:
        for token in b["attempts"][j["job_id"]]:
            state, _ = inspect_attempt(attempt_dir(batch, b, j, token), b, j)
            history[state] = history.get(state, 0) + 1
    for block, jobs in blocks(b):
        if all(j["job_id"] in good for j in jobs):
            receipts.append(receipt_status(batch, block, [good[j["job_id"]][1] for j in jobs]))
    result = dict(
        planned=len(b["jobs"]),
        attempts=sum(history.values()),
        attempt_states=history,
        complete=counts["completed"],
        counts=counts,
        paired_blocks=len(receipts),
        blocks=receipts,
        jobs=per_job,
    )
    if job:
        if job not in per_job:
            raise Failure(2, "Unknown job ID: " + job)
        return result, CODES.get(per_job[job], 40)
    return result, (30 if counts["invalid"] else (0 if counts["valid"] == len(b["jobs"]) else 40))


def probe(args, project, load, engine, analyzer):
    cfg = load(args.config)
    if args.load == "candidate" and args.scenario != "E0":
        raise Failure(2, "Only E0 may use the provisional candidate load")
    if cfg.get("resource_probe") and analyzer is None:
        analyzer = importlib.import_module("resource_probe").analyze_attempt
    if args.scenario not in cfg["scenarios"] or args.seed < 0 or args.seed in cfg["seeds"]["formal"]:
        raise Failure(2, "Select a registered scenario and a separate engineering seed")
    engine, analyzer = require_components(engine, analyzer)
    output = args.output.resolve()
    label = "%s_%s_%s_seed%d" % (args.scenario, args.load, args.scheme, args.seed)
    j = dict(
        job_id=label,
        scenario=args.scenario,
        scheme=args.scheme,
        load_id=args.load,
        seed=args.seed,
        block_id=label,
    )
    if args.density is not None:
        if not (0 < args.density < float("inf")):
            raise Failure(2, "Engineering density must be finite and positive")
        cfg = dict(cfg, density_per_lane_km=args.density)
    b = dict(
        format_version=1,
        experiment_id=safe_id(cfg["experiment_id"]),
        scope="engineering",
        config=cfg,
        sources=simulation_sources(project),
        physics_sha256=freeze_physics_sha256(cfg),
        attempts={label: []},
        jobs=[j],
    )
    with locked(output):
        if (output / "batch.json").exists():
            raise Failure(2, "Use a unique directory for each engineering probe")
        matrix = matrix_bytes([j])
        b["matrix_sha256"] = digest(matrix)
        write_atomic(output / "matrix.csv", matrix)
        p, m = run_one(output, b, j, cfg, engine, analyzer, project)
    return dict(job=label, status=m["status"], attempt=str(p), error=m.get("error")), CODES[m["status"]]


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    val = sub.add_parser("validate", help="Validate and display the effective configuration")
    val.add_argument("--config", type=Path, required=True)
    pl = sub.add_parser("plan", help="Pre-register 100 unique jobs in 25 paired blocks")
    pl.add_argument("--config", type=Path, required=True)
    pl.add_argument("--output", type=Path)
    pl.add_argument("--dry-run", action="store_true")
    pl.add_argument("--scope", choices=("formal", "synthetic"), default="formal")
    for action in ("run", "verify", "analyze", "package"):
        cmd = sub.add_parser(action)
        cmd.add_argument("--batch", type=Path, required=True)
        if action in ("run", "verify"):
            cmd.add_argument("--job")
        if action == "run":
            cmd.add_argument("--resume", action="store_true")
        if action == "package":
            cmd.add_argument("--output", required=True, type=Path)
    pr = sub.add_parser("probe", help="Run one independent engineering/evaluation seed")
    pr.add_argument("--config", required=True, type=Path)
    pr.add_argument("--output", required=True, type=Path)
    pr.add_argument("--scenario", required=True, choices=("S0", "S1", "S2", "E0"))
    pr.add_argument("--load", required=True, choices=("low", "high", "candidate"))
    pr.add_argument("--scheme", required=True, choices=SCHEMES)
    pr.add_argument("--seed", required=True, type=int)
    pr.add_argument("--density", type=float)
    c = sub.add_parser("calibrate", help="Execute P2-gated serial E0 calibration via MCP")
    c.add_argument("--config", required=True, type=Path)
    c.add_argument("--attempt", nargs="*", type=Path, help="Only evaluate the supplied attempts; never submit")
    c.add_argument("--output", type=Path)
    c.add_argument("--once", action="store_true")
    c.add_argument("--status", action="store_true")
    c.add_argument("--retry-failed", action="store_true")
    c.add_argument("--extend-timeout", type=int, help="One larger seconds budget for a progressing timed-out E0 run")
    rc = sub.add_parser("restore-check")
    rc.add_argument(
        "--checkpoint", required=True, type=Path, help="Checkpoint ZIP or JSON index (archive and SHA-256)"
    )
    rc.add_argument("--sha256")
    rc.add_argument("--destination", required=True, type=Path)
    return p


def main(argv=None, *, engine=None, analyzer=None, project=PROJECT, config_loader=read_config):
    args = parser().parse_args(argv)
    project = Path(project)
    try:
        if args.action == "validate":
            cfg = config_loader(args.config)
            result, code = dict(valid=True, physics_sha256=freeze_physics_sha256(cfg), config=cfg), 0
        elif args.action == "plan":
            result, code = plan(args, project, config_loader)
        elif args.action == "probe":
            result, code = probe(args, project, config_loader, engine, analyzer)
        elif args.action == "restore-check":
            import checkpoint

            archive = args.checkpoint.resolve()
            expected = args.sha256
            if archive.suffix.lower() == ".json":
                index = json_read(archive)
                expected = index["sha256"]
                archive = archive.parent / index["archive"]
            if not expected:
                raise Failure(2, "Supply the archived SHA-256 or a checkpoint index")
            result = checkpoint.check_archive(archive, expected, destination=args.destination.resolve())
            code = 0
        elif args.action == "calibrate":
            if args.attempt is not None:
                import calibrate
                geoms = sorted((project / "verification/p2/sumo").glob("geometry_exclusion_d*_seed*/inputs/scenario.json"))
                result = calibrate.decide(config_loader(args.config), [calibrate.evaluate(a) for a in args.attempt],
                                          "simulation", geoms)
                code = 0 if result["status"] == "selected" else 40
            else:
                import calibration_driver
                result, code = calibration_driver.run(args.config, args.output, args.once, args.retry_failed, args.status,
                                                      project=project, extend_timeout=args.extend_timeout)
        else:
            batch = args.batch.resolve()
            if not (batch / "batch.json").is_file():
                raise Failure(2, "Missing batch.json: " + str(batch))
            with locked(batch):
                b = batch_read(batch)
                if args.action == "run":
                    result, code = run(args, batch, b, project, engine, analyzer, config_loader)
                elif args.action == "verify":
                    result, code = verify(batch, b, args.job)
                elif args.action == "analyze":
                    result, code = reanalyze(batch, b, project, analyzer)
                else:
                    result, code = verify(batch, b)
                    if code == 0 and b["scope"] == "formal" and all(x["saved"] for x in result["blocks"]):
                        import checkpoint

                        if not batch.is_relative_to(project) or args.output.resolve().is_relative_to(project):
                            raise Failure(
                                2, "Package a project-local batch to an archive outside the project"
                            )
                        result = checkpoint.pack(project, args.output.resolve())
                    elif code == 0:
                        raise Failure(40, "Formal package requires all durable paired-block receipts")
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return code
    except (Failure, ImportError, FileNotFoundError, OSError, ValueError, KeyError, TypeError) as exc:
        code = (
            exc.code if isinstance(exc, Failure) else (10 if isinstance(exc, (OSError, ImportError)) else 2)
        )
        print(json.dumps(dict(error=str(exc), exit_code=code), ensure_ascii=True), file=sys.stderr)
        return code
    except KeyboardInterrupt:
        print('{"status":"interrupted","exit_code":40}', file=sys.stderr)
        return 40


if __name__ == "__main__":
    sys.exit(main())
