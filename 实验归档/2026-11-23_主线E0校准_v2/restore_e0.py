"""Restore the closed E0 evidence tree, offline, using Python 3.9 or later."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import uuid
import zipfile


BASE_SHA = "53fdc62a8dbe046fd8190226dcf60e0f99f140453b49ffb01a488cb7b771a731"
LAYERS = ("main_P2.zip", "main_P3_start.zip", "main_E0_delta.zip")
HERE = Path(__file__).resolve().parent
BASE = HERE.parent / "2026-11-22_补充实验归档/EIE575_实验完整归档.zip"
DELTA = HERE / "EIE575_E0_差异.zip"
CAMPAIGN = "results/calibration/core_v1"
BUDGET_CAMPAIGN = "results/calibration/core_v1_budget2"
BUDGET_SHA = "bac8b859d2b650b7c54af7fdda1f5bf107e5ed200de15d209640b80ac76c7e91"
FROZEN_SHA = "e11f2673af1005c75a267841338ec0f6287abef8d8b14196bb39e98525e9f550"
REVISION_IDS = ("e3e46441f32b6caa03", "e5e0f603c259082a01")
PROOF = "EIE575_E0_restore_verification.json"
DIGEST = re.compile(r"[0-9a-f]{64}")


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def json_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON key: " + key)
        result[key] = value
    return result


def decode(data):
    return json.loads(data, object_pairs_hook=json_object)


def transfer(reader, writer=None):
    digest = hashlib.sha256()
    size = 0
    while block := reader.read(1 << 20):
        digest.update(block)
        size += len(block)
        if writer is not None:
            writer.write(block)
    return {"sha256": digest.hexdigest(), "size": size}


def file_sha(path):
    with path.open("rb") as reader:
        return transfer(reader)["sha256"]


def name_parts(name):
    parts = name.split("/")
    for part in parts:
        require(part and part not in (".", "..") and part[-1] not in ". "
                and not any(ord(c) < 32 or c in '\\:<>"|?*' for c in part)
                and not re.fullmatch(r"(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[0-9¹²³]|LPT[0-9¹²³])",
                                     part.split(".")[0].rstrip(" "), re.I),
                f"Unsafe archive member {name!r}")
    return parts


def member_inventory(z, index):
    names = set()
    for info in z.infolist():
        name = info.filename
        name_parts(name)
        require(not info.is_dir() and not stat.S_ISLNK(info.external_attr >> 16)
                and name.casefold() not in names, "Invalid or duplicate ZIP member: " + name)
        names.add(name.casefold())
    meta = decode(z.read(index))
    files = meta["files"]
    return meta, files


def checked_copy(z, wire, record, path=None):
    require(isinstance(record, dict) and type(record.get("size")) is int
            and record["size"] == z.getinfo(wire).file_size
            and DIGEST.fullmatch(record.get("sha256", "")), "Invalid hash record: " + wire)
    with z.open(wire) as reader:
        if path is None:
            result = transfer(reader)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as writer:
                result = transfer(reader, writer)
    require(result == record, "File integrity mismatch: " + wire)


def extract_layers(base, temp, base_sha):
    require(file_sha(base) == base_sha, "Previous delivery SHA-256 mismatch")
    with zipfile.ZipFile(base) as z:
        meta, files = member_inventory(z, "bundle-manifest.json")
        require(meta["format"] == "eie575-consolidated-v1"
                and set(z.namelist()) == set(files) | {"bundle-manifest.json"},
                "Previous delivery inventory mismatch")
        targets = {"components/" + name: temp / name for name in LAYERS}
        require(targets.keys() <= files.keys(), "Previous delivery lacks main checkpoints")
        for name, record in files.items():
            checked_copy(z, name, record, targets.get(name))
    return {name: files["components/" + name]["sha256"] for name in LAYERS}


class Overlay:
    """Track exact spelling and type of every path in the validated overlay."""

    def __init__(self, root):
        self.root = root
        self.paths = {}
        self.files = {}

    def path(self, name, directory=False):
        parts = name_parts(name)
        for end in range(1, len(parts) + 1):
            rel = "/".join(parts[:end])
            value = (rel, directory or end < len(parts))
            require(self.paths.setdefault(rel.casefold(), value) == value,
                    "Conflicting layer path: " + rel)
        return self.root.joinpath(*parts)

    def apply(self, archive, kind, *, parent=None):
        start_delta = kind == "p3_start"
        key = "delta-manifest.json" if start_delta else "checkpoint-manifest.json"
        with zipfile.ZipFile(archive) as z:
            meta, records = member_inventory(z, key)
            if start_delta:
                require(meta["format"] == "eie575-p3-e0-delta-v1" and meta["base_p2_zip_sha256"] == parent,
                        "Incorrect P3 startup parent")
                wires = {wire: (wire[8:] if wire.startswith("project/") else None) for wire in records}
            else:
                require(meta["format"] == "eie575-checkpoint-v1", "Unknown checkpoint format")
                dirs = meta["directories"]
                require(len(set(dirs)) == len(dirs), "Duplicate checkpoint directory")
                parents = {p.rpartition("/")[0] for p in [*dirs, *records] if "/" in p}
                require(parents <= set(dirs), "Checkpoint directory inventory incomplete")
                for name in dirs:
                    self.path(name, directory=True).mkdir(parents=True, exist_ok=True)
                wires = {"project/" + name: name for name in records}
            require(set(z.namelist()) == set(wires) | {key}, "ZIP/manifest inventory mismatch")
            for wire, rel in wires.items():
                record = records[wire if start_delta else rel]
                target = self.path(rel) if rel is not None else None
                checked_copy(z, wire, record, target)
                if rel is not None:
                    self.files[rel] = record
        return len(records)

    def verify_restored_bytes(self):
        for name, expected in self.files.items():
            with (self.root / name).open("rb") as reader:
                require(transfer(reader) == expected, "Restored file mismatch: " + name)


def checked_run(root, row, archived, campaign_relative=CAMPAIGN):
    request = row["request_id"]
    rel = archived["attempt_relative"]
    parts = name_parts(rel)
    require(parts[:4] == ["results", "probes", row["probe"], request] and parts[-1] == row["attempt_id"],
            "Accepted run points outside registered job: " + request)
    attempt = root.joinpath(*parts)
    manifest = attempt / "run_manifest.json"
    require(file_sha(manifest) == row["manifest_sha256"], "Accepted run manifest changed: " + request)
    raw = decode(manifest.read_bytes())
    density, warmup, seed = row["job"]
    require(raw["attempt_id"] == row["attempt_id"] and raw["simulation_job_key"] == row["simulation_job_key"]
            and raw["status"] == "completed" and raw["data_valid"] is True and raw["data_origin"] == "simulation"
            and raw["scenario"] == "E0" and raw["scheme"] == "G0" and raw["scope"] == "engineering"
            and raw["seed"] == seed, "Accepted run identity mismatch: " + request)
    required = {"config_snapshot.json", "logs/control_log.csv", "logs/scenario_truth.csv", "logs/busy_audit.csv"}
    require(required <= raw["files"].keys(), "Accepted run missing raw evidence")
    for name, expected in raw["files"].items():
        require(file_sha(attempt.joinpath(*name_parts(name))) == expected, "Raw file mismatch: " + rel + "/" + name)
    require(row["report"] == "measurements/" + request + ".json", "Unexpected accepted report path")
    report = root / campaign_relative / row["report"]
    require(file_sha(report) == row["report_sha256"], "Accepted report hash mismatch")
    measurement = decode(report.read_bytes())
    require(measurement["valid"] is True and measurement["origin"] == "simulation"
            and (measurement["density"], measurement["warmup_s"], measurement["seed"]) == (density, warmup, seed),
            "Accepted measurement design mismatch")
    return {"request_id": request, "job": row["job"], "attempt_relative": rel,
            "raw_files_verified": len(raw["files"]), "manifest_sha256": row["manifest_sha256"],
            "measurement_sha256": row["report_sha256"]}


def campaign_path(archive):
    value = archive.get("campaign_relative", CAMPAIGN)
    for relative in (CAMPAIGN, BUDGET_CAMPAIGN):
        if value in (relative, relative.rsplit("/", 1)[1]):
            return relative
    raise ValueError("Unexpected campaign path: " + str(value))


def verify_revision_rows(v1, index, approval):
    inherited = v1["requests"]
    require(v1["status"] == "engineering_failed" and len(inherited) == 16
            and sum(q["state"] == "accepted" for q in inherited) == 10, "Unexpected frozen v1 state")
    require(index["version"] == v1["version"] and index["requests"][:-2] == inherited
            and index["budget_revision"] == approval["budget_revision"] == 2
            and index["parent_snapshot_sha256"] == approval["parent_snapshot_sha256"] == FROZEN_SHA
            and approval["science_campaign"] == v1["version"]["key"], "Revision diverged from frozen v1")
    require((approval["wall_timeout_s"], approval["additional_requests"], approval["max_parallel"]) == (5400, 2, 2)
            and approval["scope"] == "E0 engineering", "Incorrect additional request budget")
    extras = index["requests"][-2:]
    require([x["request_id"] for x in extras] == list(REVISION_IDS)
            and [x["job"] for x in extras] == [[25,20,100],[25,20,101]]
            and len({x["queue"].casefold() for x in extras}) == 2
            and len(approval["jobs"]) == 2, "Extra request identity or queue mismatch")
    for job, row in zip(approval["jobs"], extras):
        require(all(row.get(k) == v for k,v in job.items() if k != "state"), "Request changed after budget approval")
        chain = [q["request_id"] for q in inherited if q["job"] == row["job"]]
        require(chain == job["original_requests"], "Extra request does not continue its frozen chain")
    return extras


def config_physics_sha(config):
    # Matches the archived experiment.physics_fields() projection used when approving this fixed budget.
    ignored = {"stage","p3_evidence","paths","tool_paths","tools","output_root","experiment_id",
               "analysis","analysis_version","analysis_rules","metrics","reporting","calibration_evidence",
               "analysis_interpreter","scope","execution"}
    physics = {k:v for k,v in config.items() if k not in ignored}
    data = json.dumps(physics, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",",":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def verify_budget_revision(root, index, archived):
    revised = root / BUDGET_CAMPAIGN
    frozen = revised / "v1_closed"
    require(file_sha(revised/"budget_revision.json") == BUDGET_SHA and file_sha(frozen/"snapshot.json") == FROZEN_SHA,
            "Budget revision or frozen v1 snapshot differs from the original")
    snapshot = decode((frozen/"snapshot.json").read_bytes())
    for native, digest in snapshot["files"].items():
        parts = name_parts(native.replace("\\","/"))
        require(file_sha(frozen.joinpath(*parts)) == digest == file_sha((root / CAMPAIGN).joinpath(*parts)),
                "Frozen v1 file mismatch: " + native)
    v1 = decode((frozen/"index.json").read_bytes())
    approval = decode((revised/"budget_revision.json").read_bytes())
    extras = verify_revision_rows(v1, index, approval)
    frozen_count = 0
    for req, evidence in snapshot["terminal_attempts"].items():
        row = archived[req]
        parts = name_parts(row["attempt_relative"])
        require(parts[:4] == ["results","probes",row["probe"],req] and parts[-1] == row["attempt_id"]
                and row["original_mcp_state_sha256"] == evidence["mcp_receipt_sha256"], "Frozen d25 attempt changed")
        for relative, rec in evidence["raw_files"].items():
            with root.joinpath(*parts,*name_parts(relative)).open("rb") as src:
                require(transfer(src) == rec, "Frozen d25 raw evidence changed: " + relative)
            frozen_count += 1
    registry = decode((root/"configs/formal_probes.json").read_bytes())["probes"]
    for row in extras:
        req = row["request_id"]
        amended = registry[row["probe"]]
        original = registry[row["original_probe"]]
        before = (root/"configs").joinpath(*name_parts(original["config"]))
        after = (root/"configs").joinpath(*name_parts(amended["config"]))
        require(file_sha(before) == row["original_config_sha256"] and file_sha(after) == row["config_sha256"]
                and dict(original, config=amended["config"]) == amended, "Registered configuration hash mismatch")
        cfg = decode(before.read_bytes())
        cfg.setdefault("execution", {})["wall_timeout_s"] = 5400
        require(cfg == decode(after.read_bytes()) and config_physics_sha(cfg) == row["physics_sha256"],
                "Revised configuration changed outside approved wall budget")
        registration = hashlib.sha256(json.dumps(amended,sort_keys=True).encode() + after.read_bytes()).hexdigest()
        native_path = root/"archive_e0/mcp_receipts"/req/"state.json"
        native = decode(native_path.read_bytes())
        require(native["request_id"] == req and registration == row["registration_sha256"] == native["registration"]
                and native["probe"] == row["probe"] and native["scope"] == "engineering"
                and file_sha(native_path) == row["receipt_sha256"] == file_sha(revised/"receipts"/(req+".json")),
                "Native MCP registration or receipt hash differs")
        if row["state"] == "accepted":
            attempt = root.joinpath(*name_parts(archived[req]["attempt_relative"]))
            raw = decode((attempt/"run_manifest.json").read_bytes())
            actual = decode((attempt/"config_snapshot.json").read_bytes())
            batch = decode((root/"results/probes"/row["probe"]/req/"batch.json").read_bytes())
            require(actual["execution"]["wall_timeout_s"] == 5400
                    and raw["physics_sha256"] == batch["physics_sha256"] ==
                    config_physics_sha(dict(cfg,density_per_lane_km=float(row["job"][0])))
                    and batch["attempts"] == {raw["job_id"]:[row["attempt_id"]]}
                    and (not row["parent_scientific_key"] or row["parent_scientific_key"] == raw["simulation_job_key"]),
                    "Actual d25 budget, physics or unique attempt differs")
    return {"revision":2, "budget_sha256":BUDGET_SHA, "frozen_sha256":FROZEN_SHA,
            "frozen_files_verified":len(snapshot["files"]), "frozen_failure_raw_verified":frozen_count,
            "max_parallel":2, "wall_timeout_s":5400, "extra_requests":list(REVISION_IDS)}


def revision_outcome(index, decision, accepted_count, engineering_evidence_only):
    failed = [x["job"] for x in index["requests"][-2:] if x["state"] != "accepted"]
    require(accepted_count == 12 - len(failed), "Unexpected number of valid runs under budget revision")
    if failed:
        require(index["status"] == "engineering_failed" and engineering_evidence_only
                and decision["status"] == "candidate_runs_required",
                "Engineering failure; no scientific closure. Use --engineering-evidence-only for accepted evidence")
        return {"scientific_closure":False, "unresolved_jobs":failed,
                "pending_at_previous_decision":decision.get("next_runs"), "formal_simulations_authorized":False}
    require(index["status"] in ("selected","grid_exhausted"), "Revised E0 requires a terminal scientific decision")
    return {"scientific_closure":True}


def verify_closure(root, base_sha, engineering_evidence_only=False):
    archive = decode((root / "archive_e0/archive_index.json").read_bytes())
    relative = campaign_path(archive)
    campaign = root / relative / "index.json"
    outcome = root / relative / "decision.json"
    require(archive["format"] == "eie575-e0-closure-v1" and archive["phase"] == "P3_E0"
            and archive["includes_active_attempts"] is False
            and archive["parent_sha256"] == base_sha and archive["campaign_sha256"] == file_sha(campaign)
            and archive["decision_sha256"] == file_sha(outcome), "Closure metadata or parent mismatch")
    index = decode(campaign.read_bytes())
    decision = decode(outcome.read_bytes())
    require(archive["status"] == index["status"], "Final campaign status differs")
    accepted = []
    seen = set()
    archived = {r["request_id"]: r for r in archive["requests"]}
    for row in index["requests"]:
        req = row["request_id"]
        require(req not in seen and re.fullmatch(r"[A-Za-z0-9_-]+", req)
                and re.fullmatch(r"[A-Za-z0-9_-]+", row["probe"]), "Invalid request identity")
        seen.add(req)
        record = archived[req]
        require(all(record[k] == row[k] for k in ("request_id", "probe", "job", "state")),
                "Archive request differs from campaign: " + req)
        receipt = root / "archive_e0/mcp_receipts" / req / "state.json"
        require(file_sha(receipt) == record["original_mcp_state_sha256"], "MCP receipt changed: " + req)
        original = decode(receipt.read_bytes())
        require(original["request_id"] == req and
                (original.get("finished_at") or original.get("state") in ("interrupted","failed","cancelled")),
                "MCP request is not closed: " + req)
        if row["state"] == "accepted":
            accepted.append(checked_run(root, row, record, relative))
        else:
            require(row["state"] in ("engineering_failed", "invalid_measurement"), "Checkpoint includes an unfinished request: " + req)
    require(seen == set(archived) and len(seen) == len(archive["requests"]), "Request inventory mismatch")
    require(len({tuple(r["job"]) for r in accepted}) == len(accepted) == archive["accepted_count"],
            "Missing or duplicate accepted job")
    more = {"campaign_relative":relative,"scientific_decision_status":decision["status"]}
    if relative == BUDGET_CAMPAIGN:
        more["approved_budget"] = verify_budget_revision(root, index, archived)
        more.update(revision_outcome(index, decision, len(accepted), engineering_evidence_only))
        require(archive["scientific_status"] == decision["status"]
                and archive["engineering_blocked"] is (not more["scientific_closure"]),
                "Archive misclassifies scientific and engineering outcomes")
        if not more["scientific_closure"]:
            return index["status"], accepted, more
    require(decision["status"] == index["status"] and not decision.get("next_runs")
            and index["status"] not in {"submitting","running","queued","waiting","candidate_runs_required","initial_runs_required"},
            "Campaign and decision are not closed")
    if index["status"] in {"selected", "grid_exhausted", "common_warmup_failed"}:
        require(decision["status"] == index["status"] and decision["completed_unique_runs"] == len(accepted),
                "Decision does not describe the accepted set")
    more["scientific_closure"] = index["status"] in ("selected","grid_exhausted","common_warmup_failed")
    return index["status"], accepted, more


def destination_available(path):
    require(str(path).isascii() and " " not in str(path) and path.resolve() == path,
            "Use an empty short ASCII destination without spaces, junctions or aliases")
    require(not path.is_symlink() and (not path.exists() or (path.is_dir() and not any(path.iterdir()))),
            "Destination is not new or empty: " + str(path))


@contextmanager
def staging(parent):
    # Normal mkdir inherits the caller's destination ACL on Windows, including after the final rename.
    # Win32 can reject a legitimate long member even when the final restored path is short.
    # All temporary tree operations, including verification and cleanup, use the extended-length namespace.
    if os.name == "nt":
        native = str(parent.resolve())
        if not native.startswith("\\\\?\\"):
            parent = Path("\\\\?\\UNC\\" + native[2:] if native.startswith("\\\\") else "\\\\?\\" + native)
    work = parent / ("e0-" + uuid.uuid4().hex[:12])
    work.mkdir()
    try:
        yield work
    finally:
        require(work.resolve().parent == parent.resolve()
                and not (getattr(work.lstat(), "st_file_attributes", 0) & 0x400),
                "Staging directory changed identity")
        shutil.rmtree(work)


def restore(base, delta, destination, delta_sha, *, base_sha=BASE_SHA, engineering_evidence_only=False):
    base = base.resolve(strict=True)
    delta = delta.resolve(strict=True)
    dest = destination.absolute()
    destination_available(dest)
    existed = dest.exists()
    require(DIGEST.fullmatch(delta_sha) and file_sha(delta) == delta_sha, "E0 delta SHA-256 mismatch")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with staging(dest.parent) as tmp:
        root = tmp / "tree"
        root.mkdir()
        hashes = extract_layers(base, tmp, base_sha)
        overlay = Overlay(root)
        counts = {}
        for name, kind in zip(LAYERS, ("p2", "p3_start", "e0_history")):
            counts[name] = overlay.apply(tmp / name, kind, parent=hashes[LAYERS[0]])
        previous = decode((root / "checkpoint_delta/main_status.json").read_bytes())
        require([r["sha256"] for r in previous["parents"]] == [hashes[n] for n in LAYERS[:2]],
                "Historical E0 checkpoint parent mismatch")
        counts["e0_closure"] = overlay.apply(delta, "e0_closure")
        overlay.verify_restored_bytes()
        state, accepted, campaign_proof = verify_closure(root, base_sha, engineering_evidence_only)
        proof = {"passed": True, "phase":"P3_E0", "status": state, "destination":str(dest),
                 "parent_sha256":base_sha,"delta_sha256":delta_sha,"component_sha256":hashes,
                 "verified_layer_files":counts, "final_files_verified":len(overlay.files),
                 "accepted_count":len(accepted),"accepted":accepted, **campaign_proof,
                 "original_campaign_index_unchanged":True,"simulation_started":False,"automatic_resume":False}
        with (root / PROOF).open("x",encoding="utf-8") as f:
            f.write(json.dumps(proof, ensure_ascii=False, indent=2) + "\n")
        destination_available(dest)
        if existed:
            dest.rmdir()
        else:
            require(not dest.exists(), "A new destination appeared during restore")
        try:
            root.rename(dest)
        except OSError:
            if existed and not dest.exists():
                dest.mkdir()
            raise
    return proof


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--destination", type=Path, required=True)
    p.add_argument("--base", type=Path, default=BASE)
    p.add_argument("--delta", type=Path, default=DELTA)
    p.add_argument("--delta-sha256")
    p.add_argument("--engineering-evidence-only", action="store_true",
                   help="Allow restoration of a terminal engineering failure with unresolved scientific candidates")
    args = p.parse_args()
    try:
        expected = args.delta_sha256
        if expected is None:
            expected = decode((args.delta.parent / "EIE575_E0_SHA256.json").read_bytes())["delta"]["sha256"]
        receipt = restore(args.base, args.delta, args.destination, expected.lower(),
                          engineering_evidence_only=args.engineering_evidence_only)
    except (OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile) as exc:
        p.exit(2, str(exc) + "\n")
    print(json.dumps({k:receipt[k] for k in ("passed", "status", "accepted_count", "destination")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
