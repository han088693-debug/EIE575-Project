"""Package an actual four-scheme engineering block and verify a separately retrieved copy."""

import hashlib
import math
import os
from pathlib import Path
import shutil
import tempfile

import checkpoint
import experiment as ex


def require(ok, why):
    if not ok:
        raise ValueError(why)


def checked_name(root, name):
    checkpoint.validate_name(name)
    p = root / name
    require(p.resolve().is_relative_to(root.resolve()), "File outside block: " + name)
    return p


def verify_raw(path, expected_manifest):
    require(
        ex.file_hash(path / "run_manifest.json") == expected_manifest,
        "Changed attempt manifest: " + str(path),
    )
    m = ex.json_read(path / "run_manifest.json")
    require(
        m["status"] == "completed" and m["data_valid"] is True and m["scope"] == "engineering",
        "Invalid attempt",
    )
    expected = m["files"]
    observed = {f.relative_to(path).as_posix() for f in path.rglob("*") if f.is_file()}
    require(observed == set(expected) | {"run_manifest.json"}, "File inventory changed: " + str(path))
    for name, sha in expected.items():
        require(ex.file_hash(checked_name(path, name)) == sha, "Changed block file: " + name)
    return m


def put_file(src, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dest)
    except OSError:
        shutil.copyfile(src, dest)


def pack_block(project, dst, block, campaigns, cfg, *, max_part_bytes=400 * 1024**2):
    """Write atomically; reuse already verified output if the exact block was packaged."""
    dst.mkdir(parents=True, exist_ok=True)
    ordered = sorted(block, key=lambda r: r["scheme"])
    require([r["scheme"] for r in ordered] == list(ex.SCHEMES), "Four different schemes required")
    require(len({(r["scenario"], r["load_id"], r["seed"]) for r in ordered}) == 1, "Cross-block mixture")
    descriptors = [
        {k: r[k] for k in ("scheme", "attempt_id", "job", "manifest_sha256", "simulation_job_key")}
        for r in ordered
    ]
    key = ex.identity(descriptors)
    plan = {"block_key": key, "members": descriptors, "campaign": campaigns}
    final = dst / key
    if final.exists():
        index = ex.json_read(final / "upload-index.json")
        require(index["block_key"] == key, "Archive block mismatch")
        for part in index["parts"]:
            require(ex.file_hash(final / part["name"]) == part["sha256"], "Local archive part changed")
        return final / "upload-index.json"
    require(max_part_bytes > 0, "Invalid archive part limit")
    # Stage, ZIP and split parts can coexist. Reserve room even when hard links are unavailable.
    raw_bytes = sum(int(row.get("bytes_in_attempt", row["output_bytes"])) for row in ordered)
    source_bytes = sum((project / name).stat().st_size for name in campaigns["sources"])
    require(
        shutil.disk_usage(dst).free >= 3 * (raw_bytes + source_bytes) + 1024**2,
        "Insufficient temporary space for paired-block packaging",
    )
    with tempfile.TemporaryDirectory(dir=dst, prefix=".staging-") as tmp:
        staging = Path(tmp)
        tree = staging / "tree"
        build = staging / "output"
        tree.mkdir()
        build.mkdir()
        ex.save(tree / "block-index.json", plan)
        ex.save(tree / "engineering_config.json", cfg)
        for row in ordered:
            src = Path(row["attempt"])
            m = verify_raw(src, row["manifest_sha256"])
            dest = tree / "attempts" / row["scheme"] / m["attempt_id"]
            for name in [*m["files"], "run_manifest.json"]:
                put_file(checked_name(src, name), dest / name)
        for relative in campaigns["sources"]:
            p = project / relative
            require(
                p.resolve().is_relative_to(project.resolve()) and p.is_file(),
                "Missing pinned simulation code: " + relative,
            )
            require(ex.file_hash(p) == campaigns["sources"][relative], "Source changed before archive")
            put_file(p, tree / "project" / relative)
        for f in sorted((project / "tools").glob("*.py")):
            target = tree / "project" / "tools" / f.name
            if not target.exists():
                put_file(f, target)
        for f in sorted((project / "configs").glob("*.json")):
            put_file(f, tree / "project" / "configs" / f.name)
        archive = build / "paired-block.zip"
        info = checkpoint.pack(tree, archive)
        parts = []
        if archive.stat().st_size <= max_part_bytes:
            parts = [dict(name=archive.name, sha256=info["archive_sha256"], bytes=info["archive_bytes"])]
        else:
            with archive.open("rb") as payload:
                i = 0
                while payload.tell() < info["archive_bytes"]:
                    part = build / ("paired-block.zip.part%04d" % i)
                    i += 1
                    remaining = max_part_bytes
                    with part.open("xb") as out:
                        while remaining:
                            chunk = payload.read(min(remaining, 1024**2))
                            if not chunk:
                                break
                            out.write(chunk)
                            remaining -= len(chunk)
                    parts.append(dict(name=part.name, sha256=ex.file_hash(part), bytes=part.stat().st_size))
            archive.unlink()
        ex.save(
            build / "upload-index.json",
            dict(
                block_key=key,
                parts=parts,
                archive_sha256=info["archive_sha256"],
                archive_bytes=info["archive_bytes"],
                uncompressed_bytes=info["uncompressed_bytes"],
                expected_files=info["files"],
            ),
        )
        build.rename(final)
    return final / "upload-index.json"


def recovery(index_path, receipt_path, destination):
    """A receipt names each downloaded byte stream and its persistent Library identity."""
    index = ex.json_read(index_path)
    receipt = ex.json_read(receipt_path)
    require(receipt.get("block_key") == index["block_key"], "Recovery receipt references a different block")
    require(len(receipt["parts"]) == len(index["parts"]), "Missing archive parts")
    destination = destination.resolve()
    require(
        not destination.exists() or not any(destination.iterdir()), "Recovery needs a fresh empty directory"
    )
    ids = set()
    streams = []
    for expected, row in zip(index["parts"], receipt["parts"]):
        library_id = row.get("library_file_id", "")
        require(
            library_id.startswith("libfile_") and library_id not in ids,
            "Missing or duplicate persistent file ID",
        )
        ids.add(library_id)
        local = (receipt_path.parent / row["downloaded_path"]).resolve()
        require(expected["name"] == row["name"] and local.is_file(), "Missing ordered downloaded part")
        require(
            not local.samefile(index_path.parent / expected["name"]),
            "Recovery must use independently retrieved bytes",
        )
        require(
            local.stat().st_size == expected["bytes"] and ex.file_hash(local) == expected["sha256"],
            "Retrieved part integrity error",
        )
        streams.append(local)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix="p3-restore-") as t:
        if len(streams) == 1:
            archive = streams[0]
        else:
            archive = Path(t) / "restored.zip"
            with archive.open("xb") as out:
                for local in streams:
                    with local.open("rb") as data:
                        shutil.copyfileobj(data, out, 1024**2)
        details = checkpoint.check_archive(archive, index["archive_sha256"], destination=destination)
    entry = ex.json_read(destination / "block-index.json")
    require(entry["block_key"] == index["block_key"], "Restored block identity mismatch")
    for row in entry["members"]:
        root = destination / "attempts" / row["scheme"] / row["attempt_id"]
        m = verify_raw(root, row["manifest_sha256"])
        require(m["simulation_job_key"] == row["simulation_job_key"], "Restored job key mismatch")
    return dict(
        block_key=index["block_key"],
        archive_sha256=index["archive_sha256"],
        library_file_ids=sorted(ids),
        restored_to=str(destination),
        restored_files=details["files"],
        members=len(entry["members"]),
        recovery_receipt_sha256=ex.file_hash(receipt_path),
        uploaded_bytes=index["archive_bytes"],
    )


def measured_budget(rows):
    groups = {}
    for r in rows:
        groups.setdefault(r["scenario"] + "_" + r["load_id"], []).append(r)
    strata = {}
    for sc, loads in ex.SCENARIO_LOADS:
        for load in loads:
            key = sc + "_" + load
            sample = groups.get(key, [])
            require(sample, "Missing resource sample for " + key)
            peak = max(x["output_bytes"] for x in sample)
            slow = max(x["wall_seconds"] for x in sample)
            require(peak > 0 and slow > 0, "Missing actual disk/wall observation")
            strata[key] = dict(
                runs=len(sample),
                max_bytes=peak,
                max_wall_seconds=slow,
                max_private_bytes=max(x["private_bytes"] for x in sample),
                max_handles=max(x["handles"] for x in sample),
                timeout_s=math.ceil(3 * slow + 60),
            )
    global_peak = max(x["max_bytes"] for x in strata.values())
    return dict(
        strata=strata,
        observed_max_job_bytes=global_peak,
        estimated_four_job_block_bytes=4 * global_peak,
        estimated_100_job_bytes=100 * global_peak,
        required_free_bytes=200 * global_peak,
        lower_volume_estimate_bytes=20
        * sum(min(x["output_bytes"] for x in sample) for sample in groups.values()),
        duration_sum_by_stratum_s=20 * sum(x["max_wall_seconds"] for x in strata.values()),
        estimator="Global observed maximum job bytes times 100, with twice that volume reserved",
    )
