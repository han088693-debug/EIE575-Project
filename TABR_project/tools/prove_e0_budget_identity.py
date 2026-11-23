"""Verify d25 v2 against closed evidence and rebuild v1 inputs offline using netconvert only."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET

import experiment as ex
import formal_engine


ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "results/calibration/core_v1_budget2"
SNAPSHOT = "e11f2673af1005c75a267841338ec0f6287abef8d8b14196bb39e98525e9f550"
RUNTIME_FILES = {".cmdenv-log"}


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def location(raw):
    """Resolve original absolute attempt paths when running an unpacked copy elsewhere."""
    parts = Path(raw).parts
    return ROOT.joinpath(*parts[parts.index("results"):])


def names(inputs):
    names = {f.relative_to(inputs).as_posix() for f in inputs.rglob("*") if f.is_file()}
    return names - RUNTIME_FILES


def science_hashes(folder, generated):
    """Match the engine identity and also retain one comparable fingerprint per scientific file."""
    result = {}
    def portable(text):
        value = text.replace("\\","/")
        for prefix, label in ((generated, "@inputs"), (generated.parent, "@attempt")):
            value = re.sub(re.escape(prefix.as_posix()) + r"(?=/|$)", lambda _: label, value, flags=re.I)
        return value
    for name in sorted(names(folder)):
        path = folder / name
        if path.suffix.lower() in (".xml",".sumocfg",".cfg"):
            root = ET.parse(path).getroot()
            for element in root.iter():
                for key,value in element.attrib.items():
                    element.set(key,portable(value))
            result[name] = ex.digest(ET.canonicalize(ET.tostring(root,encoding="unicode")).encode("utf-8"))
        elif path.suffix.lower() == ".ini":
            lines = []
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                key,sep,value = line.partition("=")
                if sep and (key.strip() in ("result-dir","*.manager.port") or key.strip().endswith(".runId")):
                    continue
                lines.append(key+sep+portable(value) if sep else line)
            result[name] = ex.digest("\n".join(lines).encode("utf-8"))
        else:
            result[name] = sha(path)
    return result


def compare_inputs(path, generated, reference, expected):
    actual = science_hashes(path, generated)
    require(actual == expected, f"Effective inputs differ at {path}: "
            f"{[k for k in set(actual)|set(expected) if actual.get(k)!=expected.get(k)]}")
    raw = {k:sha(path/k) for k in actual}
    diff = [k for k in raw if raw[k]!=reference[k]]
    require(set(diff) <= {"highway.net.xml","traffic.launchd.xml"}, f"Unexpected raw changes {diff}")
    return dict(input_folder=path.relative_to(ROOT).as_posix(), actual_sha256=raw,
                byte_differences_from_rebuild=diff, comparable_files=len(actual),
                normalized_inputs_sha256=ex.identity(actual))


def truth(attempt):
    with (attempt/"inputs/pairs.csv").open(encoding="utf-8", newline="") as f:
        expected = {r[k] for r in csv.DictReader(f) for k in ("sender_id","receiver_id")}
    p = attempt/"logs/scenario_truth.csv"
    with p.open("rb") as f:
        data = f.read(p.stat().st_size)
    rows = csv.DictReader(io.StringIO(data[:data.rfind(b"\n")+1].decode("utf-8-sig")))
    frames = {}
    for row in rows:
        tick = round(float(row["t"])*10)
        vid = row["vehicle_id"]
        require(abs(float(row["t"])*10-tick)<1e-8, f"Off-grid truth {tick}")
        frame = frames.setdefault(tick,{})
        require(vid not in frame, f"Duplicate truth {tick}/{vid}")
        frame[vid] = [float(row[c]).hex() if row[c] else None for c in ("position_m","speed_mps","accel_mps2")]
    if frames and set(frames[max(frames)]) != expected:
        del frames[max(frames)]
    require(frames and sorted(frames)==list(range(1,max(frames)+1)) and
            all(set(frame)==expected for frame in frames.values()), "Missing complete interior truth samples")
    return frames


def older_attempts(snapshot, closed):
    prior={}
    for request,entry in snapshot["terminal_attempts"].items():
        attempt=location(entry["attempt"])
        for name, item in entry["raw_files"].items():
            f=attempt/name
            require(f.stat().st_size==item["size"] and sha(f)==item["sha256"],"Closed artifact changed: "+name)
        require(sha(closed/"requests"/request/"state.json")==entry["mcp_receipt_sha256"],
                "Closed MCP receipt changed")
        prior[request]=attempt
    return prior


def verify():
    rev=ex.json_read(DEST/"budget_revision.json")
    closed=DEST/"v1_closed"
    snap=ex.json_read(closed/"snapshot.json")
    old=ex.json_read(closed/"index.json")
    require(sha(closed/"snapshot.json")==rev["parent_snapshot_sha256"]==SNAPSHOT,"Wrong closed revision")
    for name,digest in snap["files"].items():
        require(sha(closed/name)==digest,"Closed snapshot file mismatch: "+name)
    require(rev["max_parallel"]==2 and rev["additional_requests"]==2 and rev["wall_timeout_s"]==5400 and
            rev["budget_revision"]==2 and rev["science_campaign"]==snap["campaign_key"]==old["version"]["key"],
            "Wrong campaign or allocation")
    require(sorted(r["job"] for r in rev["jobs"])==[[25,20,100],[25,20,101]],"Wrong job allocation")
    prior=older_attempts(snap,closed)
    registry=ex.json_read(ROOT/"configs/formal_probes.json")["probes"]
    version=old["version"]
    require(ex.simulation_sources(ROOT)==version["sources"],"Source modules differ from closed revision")
    results=[]
    for row in rev["jobs"]:
        seed=row["job"][-1]
        orig,new=registry[row["original_probe"]],registry[row["probe"]]
        require({**orig,"config":new["config"]}==new and new["seed"]==seed and
                new["scope"]=="engineering" and new["scheme"]=="G0" and new["scenario"]=="E0" and
                new["density_per_lane_km"]==25 and new["load_id"]=="candidate","Job registration changed")
        cfgfile,changed_file=[ROOT/"configs"/entry["config"] for entry in (orig,new)]
        cfg,changed=map(ex.json_read,(cfgfile,changed_file))
        require(sha(cfgfile)==row["original_config_sha256"] and sha(changed_file)==row["config_sha256"]
                and {**cfg,"execution":{**cfg.get("execution",{}),"wall_timeout_s":5400}}==changed,
                "Changes beyond the wall time budget")
        require(hashlib.sha256(json.dumps(new,sort_keys=True).encode()+changed_file.read_bytes()).hexdigest()
                ==row["registration_sha256"],"Registration digest mismatch")
        require(ex.freeze_physics_sha256(cfg)==row["physics_sha256"] and
                ex.runtime_hashes(cfg)==version["runtime"],"Runtime or baseline physics changed")
        root=ROOT/"results/probes"/row["probe"]/row["request_id"]
        b=ex.json_read(root/"batch.json")
        density_cfg=dict(cfg,density_per_lane_km=float(orig["density_per_lane_km"]))
        require(b["config"]=={**changed,"density_per_lane_km":25.0} and len(b["jobs"])==1 and
                b["physics_sha256"]==ex.freeze_physics_sha256(density_cfg) and
                b["sources"]==version["sources"],"Batch does not correspond to former candidate")
        label=f"E0_candidate_G0_seed{seed}"
        job=dict(job_id=label,scenario="E0",scheme="G0",load_id="candidate",seed=seed,block_id=label)
        require(b["jobs"]==[job] and len(b["attempts"][label])==1, "Unexpected matrix or attempt count")
        attempt=ex.attempt_dir(root,b,job,b["attempts"][label][0])
        m=ex.json_read(attempt/"run_manifest.json")
        baseline_snapshot=ex.job_snapshot(density_cfg,job,"engineering")
        current_snapshot=ex.json_read(attempt/"config_snapshot.json")
        require(current_snapshot=={**baseline_snapshot,"execution":b["config"]["execution"]},
                "Actual effective simulation parameters differ")
        require(m["physics_sha256"]==b["physics_sha256"] and m["sources"]==b["sources"] and
                m["scope"]=="engineering" and all(m[k]==v for k,v in job.items()),"Wrong actual manifest")
        chain=[q["request_id"] for q in old["requests"] if q["job"]==row["job"]]
        require(chain==row["original_requests"] and len(chain)==(2 if seed==100 else 0),"Wrong old request chain")
        with tempfile.TemporaryDirectory(prefix="e0proof_") as tmp:
            ref=Path(tmp)/"inputs"
            formal_engine.prepare_inputs(baseline_snapshot,job,ref)
            expected=science_hashes(ref,ref)
            reference={k:sha(ref/k) for k in expected}
            fingerprint=ex.input_fingerprint(ref)
            require(fingerprint==ex.identity(expected),"Normalization differs from experiment.py")
            key=ex.job_identity(b,job,fingerprint)
            require(m["simulation_job_key"]==key,"Rebuilt simulation_job_key differs")
            current=compare_inputs(attempt/"inputs",Path(m["generation_dir"]),reference,expected)
            previous={}
            if chain:
                live_truth=truth(attempt)
            for request in chain:
                a=prior[request]
                pm=ex.json_read(a/"run_manifest.json")
                require(pm["simulation_job_key"]==key==row["parent_scientific_key"],"Old scientific key differs")
                ar=compare_inputs(a/"inputs",Path(pm["generation_dir"]),reference,expected)
                archived=truth(a)
                end=min(max(archived),max(live_truth))
                require(all(archived[t]==live_truth[t] for t in range(1,end+1)), "Overlapping truth differs")
                prefix={str(t):live_truth[t] for t in range(1,end+1)}
                ar.update(normalized_common_truth_sha256=ex.identity(prefix),last_compared_s=end/10,
                          archived_complete_through_s=max(archived)/10,current_complete_through_s=max(live_truth)/10,
                          matched_vehicle_time_rows=sum(len(f) for f in prefix.values()))
                previous[request]=ar
            results.append(dict(seed=seed, request_id=row["request_id"], simulation_job_key=key,
                original_registered_config=orig["config"], old_registered_config_sha256=sha(cfgfile),
                amended_config_sha256=sha(changed_file), original_probe=row["original_probe"], amended_probe=row["probe"],
                configuration_difference="execution.wall_timeout_s", canonical_file_sha256=expected,
                reference_input_fingerprint=fingerprint, regenerated_old_input_sha256=reference, current=current,
                prior_attempts=previous, archived_original_attempts=len(chain),
                reference="Offline regeneration from original registered configuration and original seed; netconvert only"))
    return dict(passed=True, proof_version=1, budget_revision_sha256=sha(DEST/"budget_revision.json"),
                parent_snapshot_sha256=SNAPSHOT, closed_snapshot_files_verified=len(snap["files"]),
                closed_terminal_artifacts_verified=sum(len(x["raw_files"]) for x in snap["terminal_attempts"].values()),
                original_sources_verified=len(version["sources"]),runtime_binary_hashes_verified=version["runtime"],
                source_sha256=sha(Path(__file__)), omitted_runtime_file=".cmdenv-log",
                normalization="Generated XML comments and absolute working paths normalize under the experiment's input fingerprint. Raw CSV/JSON bytes are exact. Truth hash covers all complete overlapping frames, sorting by tick and vehicle ID, with IEEE-float position, speed and acceleration; run_id is excluded.",
                restored_rerun="python -B tools/prove_e0_budget_identity.py --output <path>",jobs=results)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=DEST/"scientific_identity_proof.json")
    args=parser.parse_args()
    out=verify()
    ex.write_atomic(args.output,(json.dumps(out,indent=2,ensure_ascii=False)+"\n").encode("utf-8"))
    print(json.dumps(dict(passed=True,output=str(args.output),sha256=sha(args.output))))
