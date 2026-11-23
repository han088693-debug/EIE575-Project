"""Exercise block retention and handoff on a copied synthetic transcript; never starts a simulation."""

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

PROJECT = Path(os.environ.get("TABR_TEST_PROJECT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(PROJECT / "tools"))
sys.path.insert(0, str(PROJECT / "tests"))

from p4_blocks import Coordinator, json_read
import experiment


class BlockLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = PROJECT
        supplied = os.environ.get("TABR_TEST_SYNTHETIC_BATCH")
        if supplied:
            cls.baseline = Path(supplied)
            return
        from formal_analyze import analyze_attempt
        from formal_fixtures import fixture_config
        cls.fixtures = tempfile.TemporaryDirectory(prefix="tabr-fixture-source-")
        cls.addClassCleanup(cls.fixtures.cleanup)
        root = Path(cls.fixtures.name)
        cls.baseline = root / "results" / "batches" / "fixture"
        cfg = fixture_config()
        cfg.update(stage="synthetic", experiment_id="p4_block_fixture")
        config_file = root / "synthetic.json"
        experiment.save(config_file, cfg)
        experiment.plan(SimpleNamespace(config=config_file, scope="synthetic", dry_run=False,
                                        output=cls.baseline), PROJECT, json_read)
        b = experiment.batch_read(cls.baseline)
        engine = experiment.synthetic_engine(PROJECT)
        for job in next(iter(experiment.blocks(b)))[1]:
            _, m = experiment.run_one(cls.baseline, b, job, cfg, engine, analyze_attempt, PROJECT)
            if m["status"] != "completed":
                raise ValueError("Synthetic fixture failed: " + str(m.get("error")))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tabr_block_test_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.batch = self.root / "results" / "batches" / "test"
        shutil.copytree(self.baseline, self.batch)
        self.coord = Coordinator(self.project, self.batch, synthetic=True)
        source = Coordinator(self.project, self.baseline, synthetic=True)
        b = source.registration()
        jobs = next(iter(source.ep.blocks(b)))[1]
        for j in jobs:
            old = source.ep.attempt_dir(self.baseline, b, j, b["attempts"][j["job_id"]][0])
            dest = self.coord.ep.attempt_dir(self.batch, b, j, b["attempts"][j["job_id"]][0])
            shutil.copytree(old, dest)
        self.output = self.root / "archives"

    def test_integrity_roundtrip_and_repeat(self):
        coord = self.coord
        first = coord.pack(self.output)
        again = coord.pack(self.output)
        self.assertEqual(first["archive_sha256"], again["archive_sha256"])
        archive = Path(first["archive"])
        dest = self.root / "restored"
        verified = coord.cp.check_archive(archive, first["archive_sha256"], destination=dest)
        self.assertGreater(verified["files"], 60)
        identity = json_read(dest / "paired_block.json")
        self.assertEqual(len(identity["selected"]), 4)
        for f, record in identity["archived_file_sha256"].items():
            self.assertEqual(coord.cp.file_hash(dest / f), record["sha256"])
        # Exercise local receipt validation without remote I/O, in a disposable copy only.
        mock = self.root / "mock_create_result.json"
        receipt = dict(
            filename=archive.name,
            library_file_id="libfile_mock_synthetic_test_only",
            size_bytes=archive.stat().st_size,
            archive_sha256=first["archive_sha256"],
        )
        mock.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Synthetic"):
            coord.ack(archive, mock)
        self.assertFalse((self.batch / "checkpoints").exists())
        proof = coord.record_receipt(archive, mock)
        self.assertTrue(Path(proof["receipt"]).exists())
        self.assertTrue(coord.saved_index(coord.registration(), first["block_id"]))
        receipt["archive_sha256"] = "0" * 64
        mock.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            coord.record_receipt(archive, mock)
        self.assertEqual(json_read(Path(proof["receipt"]))["archive_sha256"], first["archive_sha256"])

    def test_incomplete_block_is_not_archived(self):
        b = self.coord.registration()
        j = next(iter(self.coord.ep.blocks(b)))[1][0]
        p = self.coord.ep.attempt_dir(self.batch, b, j, b["attempts"][j["job_id"]][0])
        (p / "metrics.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.coord.status()["state"], "ready_to_run")
        with self.assertRaisesRegex(ValueError, "No completed"):
            self.coord.pack(self.output)
        self.assertFalse(list(self.output.glob("*.zip")))

    def test_unfrozen_formal_registry_is_rejected(self):
        b = experiment.batch_read(self.batch)
        b["scope"] = "formal"
        b["config"]["stage"] = "candidate"
        experiment.save(self.batch / "batch.json", b)
        with self.assertRaisesRegex(ValueError, "frozen configuration"):
            Coordinator(self.project,self.batch).status()
        b["config"]["stage"] = "frozen"
        b["config"]["p3_evidence"] = {"path":"p3_evidence.json","sha256":"0"*64}
        experiment.save(self.batch / "batch.json", b)
        experiment.save(self.batch / "p3_evidence.json", {"passed":True,"phase":"P3"})
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            Coordinator(self.project,self.batch).status()

    def test_direct_job_cannot_skip_prior_incomplete_block(self):
        b = experiment.batch_read(self.batch)
        b["scope"] = "formal"
        b["config"]["stage"] = "frozen"
        b["config"]["p3_evidence"] = {"path":"p3_evidence.json","sha256":"0"*64}
        # Isolate ordering from upstream freeze validation; no engine can be invoked.
        b["config_origin"] = str(self.root / "removed-source-config.json")
        j0=b["jobs"][0]
        b["attempts"][j0["job_id"]] = []
        later=b["jobs"][4]["job_id"]
        engine = SimpleNamespace(prepare_inputs=lambda *args: None, run_engine=lambda *args: None)
        with patch.object(experiment,"check_gate"), patch.object(experiment,"run_one") as launched:
            with self.assertRaisesRegex(experiment.Failure,"earlier paired block") as exc:
                experiment.run(SimpleNamespace(job=later,resume=True),self.batch,b,self.project,engine,
                               lambda p: None,json_read)
            self.assertEqual(exc.exception.code,40)
            launched.assert_not_called()

    def test_archive_rejects_external_substitution(self):
        from zipfile import ZipFile

        record = self.coord.pack(self.output)
        archive = Path(record["archive"])
        with ZipFile(archive, "a") as z:
            z.writestr("untracked.csv", "synthetic")
        with self.assertRaises(ValueError):
            self.coord.pack(self.output)


if __name__ == "__main__":
    unittest.main()
