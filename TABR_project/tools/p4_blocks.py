"""Run one registered four-scheme TABR block and checkpoint its complete raw attempts.

Use status, run-block, pack, then record the persistent-file service's successful
create result with ack before advancing. This CLI never uploads files.
"""

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4
import zipfile


def require(ok, message):
    if not ok:
        raise ValueError(message)


def file_record(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for piece in iter(lambda: f.read(1024 * 1024), b""):
            h.update(piece)
    return {"size": path.stat().st_size, "sha256": h.hexdigest()}


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def json_read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


class Coordinator:
    def __init__(self, project, batch, *, synthetic=False):
        self.project = project.resolve()
        self.batch = batch.resolve()
        self.synthetic = synthetic
        sys.path.insert(0, str(self.project / "tools"))
        import checkpoint, experiment

        self.ep = experiment
        self.cp = checkpoint
        require((self.batch / "batch.json").is_file(), "Batch registration missing")
        require(self.batch.parent.name == "batches", "Use a registered results/batches directory")

    def registration(self):
        try:
            b = self.ep.batch_read(self.batch)
        except self.ep.Failure as exc:
            raise ValueError(str(exc)) from exc
        require(b["scope"] == ("synthetic" if self.synthetic else "formal"), "Unexpected batch scope")
        if not self.synthetic:
            receipt = dict(b["config"].get("p3_evidence") or {}, path="p3_evidence.json")
            try:
                self.ep.check_gate(dict(b["config"], p3_evidence=receipt), self.batch / "config.json")
            except self.ep.Failure as exc:
                raise ValueError(str(exc)) from exc
            require(
                self.batch.parent == self.project / "results/batches",
                "MCP operates on the project-local formal batch",
            )
        return b

    def block(self, b, block_id):
        jobs = next((jobs for name, jobs in self.ep.blocks(b) if name == block_id), None)
        require(
            jobs and [j["scheme"] for j in jobs] == list(self.ep.SCHEMES),
            "Missing registered G0–G3 block",
        )
        chosen = []
        for job in jobs:
            status, folder, manifest = self.ep.selected(self.batch, b, job)
            chosen.append((job, status, folder, manifest))
        return chosen

    def identity(self, b, block_id, chosen):
        require(all(m for _, _, _, m in chosen), "All four validated attempts are required")
        trajectories = []
        for j, _, path, m in chosen:
            metrics = json_read(path / "metrics.json")
            expected_events = b["config"]["scenarios"][j["scenario"]]["events"]
            require(
                metrics["data_validity"] == "valid"
                and metrics["run_id"] == m["attempt_id"]
                and metrics["data_origin"] == ("synthetic" if self.synthetic else "simulation")
                and metrics["run"]["event_pair_n"] == expected_events,
                "Bad attempt provenance/event denominator",
            )
            trajectories.append(metrics["state"]["trajectory_sha256_by_vehicle"])
        require(
            trajectories[0] and all(v == trajectories[0] for v in trajectories),
            "Unpaired target/background trajectories",
        )
        official = self.ep.receipt_status(self.batch, block_id, [m for _, _, _, m in chosen])
        return {
            "format": "eie575-paired-block-v1",
            "scope": b["scope"],
            "block_id": block_id,
            "block_key": official["block_key"],
            "matrix_sha256": b["matrix_sha256"],
            "physics_sha256": b["physics_sha256"],
            "experiment_id": b["experiment_id"],
            "trajectory_hash_set_sha256": digest_json(trajectories[0]),
            "selected": {j["job_id"]: m["attempt_id"] for j, _, _, m in chosen},
            "prior_attempts": {j["job_id"]: b["attempts"][j["job_id"]] for j, _, _, _ in chosen},
        }

    def status(self):
        b = self.registration()
        for name, _ in self.ep.blocks(b):
            if not self.synthetic:
                saved = self.saved_index(b, name)
                if saved:
                    continue
            chosen = self.block(b, name)
            missing = [
                {"job": j["job_id"], "status": state, "prior_attempts": len(b["attempts"][j["job_id"]])}
                for j, state, _, m in chosen
                if m is None
            ]
            if missing:
                return {
                    "state": "ready_to_run",
                    "block_id": name,
                    "missing": missing,
                    "scope": b["scope"],
                }
            # Verify the current raw evidence of every previously checkpointed group.
            row = self.identity(b, name, chosen)
            if (
                self.synthetic
                or not self.ep.receipt_status(self.batch, name, [m for _, _, _, m in chosen])["saved"]
            ):
                return {"state": "awaiting_checkpoint", "scope": b["scope"], **row}
        return {"state": "matrix_checkpointed", "blocks": 25, "scope": b["scope"]}

    def saved_index(self, b, name):
        # Released blocks are immutable. Recheck manifest snapshots during scheduling;
        # full independent raw byte verification is performed at pack/ack and final verification.
        check = self.batch / "checkpoints" / (name + ".json")
        if not check.is_file():
            return False
        proof = json_read(check)
        selected = proof.get("selected")
        if not selected:
            return False
        jobs = next(jobs for block_id, jobs in self.ep.blocks(b) if block_id == name)
        ms = []
        for j in jobs:
            token = selected.get(j["job_id"])
            require(token in b["attempts"][j["job_id"]], "Durable attempt no longer registered")
            m = json_read(self.ep.attempt_dir(self.batch, b, j, token) / "run_manifest.json")
            require(m["status"] == "completed" and m["data_valid"], "Archived run invalidated")
            ms.append(m)
        require(
            self.ep.receipt_status(self.batch, name, ms)["saved"],
            "Saved block manifest no longer matches receipt",
        )
        return True

    def archive_files(self, b, chosen):
        # Preserve earlier engineering failures and invalid attempts in the same block.
        files = {}
        directories = set()
        # Paths within each package are relative to the project's results/ directory.
        for j, _, _, _ in chosen:
            for token in b["attempts"][j["job_id"]]:
                path = self.ep.attempt_dir(self.batch, b, j, token)
                require(path.is_dir(), "Missing registered attempt: " + str(path))
                for f in path.rglob("*"):
                    require(not self.cp.reparse_point(f), "Nonportable link in attempt")
                    rel = f.relative_to(self.batch.parent.parent).as_posix()
                    if f.is_file():
                        files[rel] = f
                    elif f.is_dir():
                        directories.add(rel)
        # Snapshot the live registration to reconstruct the source matrix and exact chosen attempts.
        for name in ("batch.json", "matrix.csv", "config.json", "p3_evidence.json"):
            f = self.batch / name
            if f.exists():
                files["batches/" + self.batch.name + "/" + name] = f
        return files, directories

    def pack(self, out):
        out = out.resolve()
        require(not out.is_relative_to(self.batch.parent.parent), "Package outside the results tree")
        out.mkdir(parents=True, exist_ok=True)
        with self.ep.locked(self.batch):
            state = self.status()
            require(
                state["state"] == "awaiting_checkpoint",
                "No completed next block is awaiting a checkpoint",
            )
            b = self.registration()
            chosen = self.block(b, state["block_id"])
            marker = self.identity(b, state["block_id"], chosen)
            path = out / ("TABR_" + marker["block_id"] + "_" + marker["block_key"][:16] + ".zip")
            if path.exists():
                self.verify_contents(path, None, marker, chosen, b)
                return {
                    **marker,
                    "archive": str(path),
                    "archive_sha256": self.cp.file_hash(path),
                    "bytes": path.stat().st_size,
                }
            files, directories = self.archive_files(b, chosen)
            references = {k: file_record(f) for k, f in files.items()}
            marker["archived_file_sha256"] = references
            with TemporaryDirectory(prefix="tabr-pair-", dir=out) as name:
                stage = Path(name) / "block"
                stage.mkdir()
                for entry in directories:
                    (stage / entry).mkdir(parents=True, exist_ok=True)
                for k, f in files.items():
                    target = stage / k
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.link(f, target)
                    except OSError:
                        shutil.copyfile(f, target)
                self.ep.save(stage / "paired_block.json", marker)
                temp = Path(name) / "block.zip"
                info = self.cp.pack(stage, temp)
                require(self.cp.file_hash(temp) == info["archive_sha256"], "Invalid staged archive")
                # A normal new file inherits the destination's Windows ACL for the subsequent host upload.
                working = out / ("." + uuid4().hex + ".tmp")
                try:
                    with temp.open("rb") as source, working.open("xb") as dest:
                        shutil.copyfileobj(source, dest, 1024 * 1024)
                    require(
                        self.cp.file_hash(working) == info["archive_sha256"], "Delivery copy mismatch"
                    )
                    if os.name == "nt":
                        working.rename(path)
                    else:
                        os.link(working, path)
                finally:
                    working.unlink(missing_ok=True)
            return {
                "block_id": marker["block_id"],
                "block_key": marker["block_key"],
                "scope": b["scope"],
                "archive": str(path),
                "archive_sha256": info["archive_sha256"],
                "bytes": path.stat().st_size,
            }

    def verify_contents(self, path, expected_hash, identity, chosen, b):
        self.cp.check_archive(path, expected_hash)
        with zipfile.ZipFile(path) as bundle:
            manifest = json.loads(bundle.read(self.cp.INDEX))
            included = manifest["files"]
            marker = json.loads(bundle.read(self.cp.PREFIX + "paired_block.json"))
        require(all(marker[k] == v for k, v in identity.items()), "Archive block identity mismatch")
        entries = marker["archived_file_sha256"]
        require(
            set(included) == set(entries) | {"paired_block.json"}
            and all(included[k] == v for k, v in entries.items()),
            "Incomplete block archive inventory",
        )
        # The batch control index may have advanced on resume. All original attempt files must still match.
        current, _ = self.archive_files(b, chosen)
        require(set(current) == set(entries), "Attempt files changed since pack")
        for k, f in current.items():
            if k == "batches/" + self.batch.name + "/batch.json":
                continue
            require(file_record(f) == entries[k], "Changed source since pack: " + k)

    def ack(self, archive, upload):
        require(
            not self.synthetic, "Synthetic data cannot be acknowledged with a durable formal receipt"
        )
        return self.record_receipt(archive, upload)

    def record_receipt(self, archive, upload):
        path = archive.resolve()
        receipt = json_read(upload)
        native = receipt.get("library_file_id", "")
        expected = receipt.get("archive_sha256", "")
        require(
            re.fullmatch(r"libfile_[A-Za-z0-9_-]+", native)
            and re.fullmatch(r"[0-9a-fA-F]{64}", expected),
            "A successful native Library create receipt and the original saved SHA-256 are required",
        )
        require(
            receipt.get("filename", receipt.get("file_name", receipt.get("name"))) == path.name,
            "Receipt does not name this archive",
        )
        if receipt.get("size_bytes") is not None:
            require(receipt["size_bytes"] == path.stat().st_size, "Receipt size mismatch")
        with self.ep.locked(self.batch):
            b = self.registration()
            with zipfile.ZipFile(path) as z:
                marker = json.loads(z.read(self.cp.PREFIX + "paired_block.json"))
            name = marker["block_id"]
            chosen = self.block(b, name)
            current = self.identity(b, name, chosen)
            self.verify_contents(path, expected, current, chosen, b)
            dest = self.batch / "checkpoints" / (name + ".json")
            proof = dict(
                block_id=name,
                block_key=current["block_key"],
                archive_sha256=expected.lower(),
                library_file_id=native,
                archive=path.name,
                size_bytes=path.stat().st_size,
                selected=current["selected"],
            )
            if receipt.get("file_id"):
                proof["file_id"] = receipt["file_id"]
            if dest.exists():
                existing = json_read(dest)
                require(
                    all(
                        existing.get(k) == proof[k]
                        for k in ("library_file_id", "block_key", "archive_sha256")
                    ),
                    "Existing checkpoint receipt conflicts; no replacement made",
                )
            else:
                state = self.status()
                require(
                    state["state"] == "awaiting_checkpoint" and state["block_id"] == name,
                    "Checkpoint must follow registered block order",
                )
                self.ep.save(dest, proof)
            require(
                self.ep.receipt_status(self.batch, name, [m for _, _, _, m in chosen])["saved"],
                "Receipt failed verification",
            )
            return {"acknowledged": name, "receipt": str(dest), "library_file_id": native}

    async def run_block(self, retry=False):
        require(not self.synthetic, "run-block submits only registered formal experiments")
        state = self.status()
        if state["state"] != "ready_to_run":
            return state
        b = self.registration()
        nonce = digest_json([b["physics_sha256"], b["matrix_sha256"], b["sources"]])[:10]
        tools = b["config"]["tools"]
        sys.path.insert(0, tools["mcp_dir"])
        from client import connect, result_data

        async with connect(self.project / "tools/formal_mcp.py") as session:

            async def call(name, **kwargs):
                return result_data(await session.call_tool(name, kwargs))

            health = await call("get_status")
            require(
                health["host"]["ready"] and Path(health["batch_root"]).resolve() == self.batch.parent,
                "Formal MCP host is offline or points to another batch root",
            )
            submitted = []
            for item in state["missing"]:
                job = item["job"]
                # An MCP request can survive client disconnection before the attempt is recorded.
                # Reattach using the submission receipt before allocating a new request ID.
                record = self.batch / "control" / (job + ".json")
                old = json_read(record) if record.is_file() else None
                previous = None
                if old:
                    try:
                        previous = await call("get_status", request_id=old["request_id"])
                    except RuntimeError:
                        # This journal entry was created before the original submit was committed.
                        if old.get("submitted"):
                            raise
                if previous and previous["state"] in {"failed", "cancelled", "interrupted", "completed"}:
                    require(
                        retry, "Earlier request ended without a valid result; verify it and pass --retry"
                    )
                    revision = old["revision"] + 1
                else:
                    revision = old["revision"] if old else 0
                    require(
                        not (item["prior_attempts"] and not old and not retry),
                        "Incomplete earlier attempt; inspect then pass --retry",
                    )
                request = f"p4_{nonce}_{job}_{revision}"
                ticket = {"request_id": request, "revision": revision}
                self.ep.save(record, ticket)
                response = await call(
                    "start_run", batch=self.batch.name, job=job, request_id=request, resume=True
                )
                self.ep.save(record, dict(ticket, submitted=True))
                while response["state"] not in {"completed", "failed", "cancelled", "interrupted"}:
                    await asyncio.sleep(2)
                    response = await call("get_status", request_id=request)
                result = await call("get_results", request_id=request)
                self.ep.save(self.batch / "control" / "history" / (request + ".json"), result)
                require(
                    result.get("complete"),
                    f"{job} did not complete: {result.get('attempt_error') or result.get('error') or response['state']}",
                )
                submitted.append({"job_id": job, "request_id": request, "attempt": result["attempt"]})
        end = self.status()
        require(
            end["state"] == "awaiting_checkpoint" and end["block_id"] == state["block_id"],
            "Paired block did not pass the checkpoint gate",
        )
        return {**end, "processed": submitted}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument(
        "--synthetic", action="store_true", help="Inspect or package synthetic fixtures only"
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("status")
    run = sub.add_parser("run-block")
    run.add_argument(
        "--retry", action="store_true", help="Retain the previous failed attempt and issue a new request"
    )
    pack = sub.add_parser("pack")
    pack.add_argument(
        "--out", type=Path, required=True, help="Directory outside the project results tree"
    )
    ack = sub.add_parser("ack")
    ack.add_argument("--archive", type=Path, required=True)
    ack.add_argument(
        "--upload",
        type=Path,
        required=True,
        help="Successful saved-file receipt JSON: filename, library_file_id, file_id, size_bytes and original archive_sha256",
    )
    args = parser.parse_args()
    try:
        if args.action == "status" and not (args.batch / "batch.json").is_file():
            frozen = args.project / "configs/frozen_core.json"
            state = "awaiting_p3_freeze"
            if frozen.is_file():
                sys.path.insert(0, str(args.project.resolve() / "tools"))
                import experiment
                try:
                    experiment.check_gate(json_read(frozen), frozen)
                except experiment.Failure as exc:
                    raise ValueError(str(exc)) from exc
                state = "awaiting_batch_registration"
            print(json.dumps({"state": state, "batch": str(args.batch.resolve()),
                              "registered": False, "formal_jobs_submitted": 0}))
            return
        coord = Coordinator(args.project, args.batch, synthetic=args.synthetic)
        if args.action == "status":
            result = coord.status()
        elif args.action == "run-block":
            # Separate controller lease; experiment acquires its own batch lock inside each launched process.
            with coord.ep.locked(coord.batch / "control"):
                result = asyncio.run(coord.run_block(args.retry))
        elif args.action == "pack":
            result = coord.pack(args.out)
        else:
            result = coord.ack(args.archive, args.upload)
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    except (RuntimeError, ValueError, KeyError, OSError, TypeError, zipfile.BadZipFile) as exc:
        parser.exit(30, str(exc) + "\n")


if __name__ == "__main__":
    main()
