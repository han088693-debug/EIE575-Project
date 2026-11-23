"""Read locally attested, accepted E0 measurements without touching a queue or simulation."""
import argparse
import hashlib
import json
from pathlib import Path

from initial_wall_budget import select_initial_budget


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def stored_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pinned_e0_benchmarks(evidence_path, expected_sha256, *, project, machine_id):
    """Resolve an explicitly trusted evidence fingerprint, then remeasure from signed artifact paths."""
    evidence_path, project = Path(evidence_path), Path(project)
    if stored_hash(evidence_path) != expected_sha256:
        raise ValueError("Unrecognized benchmark evidence SHA-256")
    evidence = load(evidence_path)
    if (evidence["format"]!="e0-closed-serial-wall-v1" or evidence["machine_id"]!=machine_id or
            evidence["cost_group"]!="E0/G0/registered_pairs" or evidence["measured_slots"]!=1):
        raise ValueError("Unexpected benchmark origin, class or concurrency")
    closed = (project / evidence["closed_dir"]).resolve()
    if not closed.is_relative_to(project.resolve()):
        raise ValueError("Benchmark path does not belong to the restored project")
    snapfile, index_path = closed/"snapshot.json", closed/"index.json"
    if stored_hash(snapfile)!=evidence["closed_snapshot_sha256"]:
        raise ValueError("Benchmark closed snapshot has changed")
    snapshot = load(snapfile)
    if stored_hash(index_path)!=snapshot["files"]["index.json"]:
        raise ValueError("Closed E0 index SHA-256 mismatch")
    serial = load(index_path)
    if serial.get("budget_revision") or serial["phase"]!="E0" or snapshot["campaign_key"]!=serial["version"]["key"]:
        raise ValueError("Not an original serial E0 campaign")
    slots = {r["request_id"]:1 for r in serial["requests"] if r["state"]=="accepted"}
    return accepted_e0_benchmarks(index_path,project=project,machine_id=machine_id,
                                  cost_group=evidence["cost_group"],slots_by_request=slots)


def accepted_e0_benchmarks(index_path, *, project, machine_id, cost_group, slots_by_request):
    """Caller attests locality and historical concurrency. Unknown admission is never assumed single-slot."""
    index_path, project = Path(index_path), Path(project)
    rows = []
    for record in load(index_path)["requests"]:
        request = record["request_id"]
        if record["state"] != "accepted" or request not in slots_by_request:
            continue
        original = Path(record["attempt"])
        parts = original.parts
        attempt = project.joinpath(*parts[parts.index("results"):])
        report_path = index_path.parent / record["report"]
        manifest_path = attempt/"run_manifest.json"
        if stored_hash(report_path) != record["report_sha256"] or stored_hash(manifest_path) != record["manifest_sha256"]:
            raise ValueError("Accepted manifest/measurement hash mismatch for "+request)
        report, manifest = load(report_path), load(manifest_path)
        engine_path = attempt/"logs/engine_status.json"
        snapshot_path = attempt/"config_snapshot.json"
        for path in (engine_path,snapshot_path):
            if stored_hash(path) != manifest["files"][path.relative_to(attempt).as_posix()]:
                raise ValueError("Engine or configuration hash mismatch for "+request)
        engine, snapshot = load(engine_path), load(snapshot_path)
        if (report["valid"] is not True or manifest["data_valid"] is not True or
                report["origin"]!="simulation" or manifest["data_origin"]!="simulation" or
                manifest["scope"]!="engineering" or manifest["scenario"]!="E0" or manifest["scheme"]!="G0" or
                manifest["status"]!="completed" or snapshot["observation_mode"]!="registered_pairs" or
                engine["engine_complete"] is not True or engine["return_code"]!=0 or engine["status"]!="completed" or
                manifest["simulation_job_key"]!=record["simulation_job_key"] or manifest["attempt_id"]!=record["attempt_id"] or
                [report["density"],report["warmup_s"],report["seed"]] != record["job"] or
                snapshot["density_per_lane_km"]!=report["density"] or snapshot["seed"]!=report["seed"]):
            raise ValueError("Incomplete or noncomparable E0 benchmark for "+request)
        rows.append(dict(attempt_id=manifest["attempt_id"], machine_id=machine_id, cost_group=cost_group,
                         accepted=True, density=report["density"], duration_s=snapshot["timing"]["duration_s"],
                         slots=slots_by_request[request], wall_seconds=engine["wall_seconds"],
                         request_id=request, engine_sha256=stored_hash(engine_path)))
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--closed-v1-index", required=True, type=Path,
                        help="Closed serial E0 index from the named machine, excluding subsequent parallel attempts")
    parser.add_argument("--machine-id", required=True)
    parser.add_argument("--cap-s", required=True, type=int)
    parser.add_argument("--density", type=float, required=True)
    parser.add_argument("--slots", type=int, required=True)
    options = parser.parse_args()
    using = dict(machine_id=options.machine_id, cost_group="E0/G0/registered_pairs")
    serial = load(options.closed_v1_index)
    if serial.get("budget_revision") or serial.get("phase")!="E0":
        parser.error("A closed serial v1 index is required. Supply per-attempt slot evidence for mixed campaigns.")
    runs = accepted_e0_benchmarks(options.closed_v1_index,project=options.project,
                                  slots_by_request={r["request_id"]:1 for r in serial["requests"]},**using)
    print(json.dumps(select_initial_budget(runs,density=options.density,duration_s=120,slots=options.slots,
                                           authorized_cap_s=options.cap_s,**using),indent=2))
