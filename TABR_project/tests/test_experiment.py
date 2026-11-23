"""Run as python tests/test_experiment.py. All artifacts use temporary synthetic data."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import experiment as ex


class TestEngine:
    def __init__(self):
        self.mode = "miss"
        self.calls = []

    def prepare_inputs(self, config, job, dest):
        (dest / "events.csv").write_text("event\n" + ("" if job["scenario"] == "S0" else "e\n"))
        (dest / "launch.xml").write_text('<launch><basedir path="%s"/></launch>' % dest.as_posix())

    def run_engine(self, config, job, attempt):
        self.calls.append(job["job_id"])
        if self.mode == "interrupt":
            raise KeyboardInterrupt
        if self.mode == "crash":
            raise RuntimeError("engine stopped")
        (attempt / "logs" / "out.csv").write_text(self.mode + "\n")
        return {"engine": "test fixture"}


def analyze(path):
    mode = (path / "logs" / "out.csv").read_text().strip()
    if mode == "invalid":
        raise ValueError("invalid row")
    return dict(delivered=False, deadline_miss=True)


class ExperimentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = self.root / "config.json"
        self.b = self.root / "results" / "batches" / "test"
        self.engine = TestEngine()
        ex.save(
            self.cfg,
            {
                "stage": "candidate",
                "experiment_id": "test",
                "seeds": {"formal": list(range(5))},
                "control": {"rmax": 10},
                "analysis": {"endpoint": "time"},
                "scenarios": {s: {} for s in ("S1", "S0", "S2")},
            },
        )

    def invoke(self, *args, expected=0):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ex.main(
                list(map(str, args)),
                engine=self.engine,
                analyzer=analyze,
                project=self.root,
                config_loader=ex.json_read,
            )
        self.assertEqual(code, expected, (out.getvalue(), err.getvalue()))
        return json.loads(out.getvalue() or err.getvalue())

    def new(self, scope="synthetic"):
        return self.invoke("plan", "--config", self.cfg, "--scope", scope, "--output", self.b)

    def run_job(self, job="S1_low_G0_seed0", *more, expected=0):
        return self.invoke("run", "--batch", self.b, "--job", job, *more, expected=expected)

    def test_matrix_and_identities(self):
        p = self.invoke("plan", "--dry-run", "--config", self.cfg, "--output", self.b)
        self.assertFalse(self.b.exists())
        self.assertEqual((p["formal_jobs"], p["paired_blocks"], p["duplicate_jobs"]), (100, 25, 0))
        self.assertEqual([j["scenario"] for j in p["jobs"]], ["S1"] * 40 + ["S0"] * 40 + ["S2"] * 20)
        self.assertEqual(len(set(j["job_id"] for j in p["jobs"])), 100)
        cfg = ex.json_read(self.cfg)
        key = ex.freeze_physics_sha256(cfg)
        self.assertEqual(
            key, ex.freeze_physics_sha256(dict(cfg, tools={"root": "X:/"}, analysis={"endpoint": "x"}))
        )
        self.assertNotEqual(key, ex.freeze_physics_sha256(dict(cfg, control={"rmax": 2})))

    def test_formal_freeze_and_archive(self):
        self.invoke("plan", "--config", self.cfg, "--output", self.b, expected=2)
        self.assertFalse(self.b.exists())
        b2 = self.root / "results" / "batches" / "frozen"
        cfg = ex.json_read(self.cfg)
        cfg["stage"] = "frozen"
        receipt = self.root / "p3.json"
        ex.save(receipt, dict(phase="P3", passed=True, freeze_physics_sha256=ex.freeze_physics_sha256(cfg)))
        cfg["p3_evidence"] = dict(path="p3.json", sha256=ex.file_hash(receipt))
        ex.save(self.cfg, cfg)
        self.b = b2
        self.new("formal")
        self.assertEqual((b2 / "p3_evidence.json").read_bytes(), receipt.read_bytes())
        # Existing candidate registrations from an older planner must still fail at execution.
        batch = ex.json_read(b2 / "batch.json")
        old = dict(batch, config=dict(batch["config"], stage="candidate", p3_evidence=None))
        ex.save(b2 / "batch.json", old)
        self.run_job(expected=2)
        self.assertFalse(self.engine.calls)
        ex.save(b2 / "batch.json", batch)
        pending = self.invoke("run", "--batch", self.b, expected=40)
        self.assertEqual(len(self.engine.calls), 4)
        self.assertFalse(pending["saved"])
        blocked = self.invoke("run", "--batch", self.b, "--job", "S1_high_G0_seed0", expected=40)
        self.assertEqual(blocked["status"], "awaiting_checkpoint")
        self.assertEqual(len(self.engine.calls), 4)
        # A valid receipt for another block cannot release this one.
        path = Path(pending["receipt"])
        rec = dict(library_file_id="libfile_fixture", archive_sha256="f" * 64, block_key="0" * 64)
        ex.save(path, rec)
        self.invoke("run", "--batch", self.b, "--resume", expected=40)
        self.assertEqual(len(self.engine.calls), 4)
        ex.save(path, dict(rec, block_key=pending["block_key"]))
        self.invoke("run", "--batch", self.b, "--resume", expected=40)
        self.assertEqual(len(self.engine.calls), 8)

    def test_performance_failures_and_full_batch(self):
        self.new()
        self.invoke("run", "--batch", self.b)
        v = self.invoke("verify", "--batch", self.b)
        self.assertEqual(v["counts"]["valid"], 100)
        self.assertEqual(v["paired_blocks"], 25)
        self.invoke("run", "--batch", self.b, "--resume")
        self.assertEqual(len(self.engine.calls), 100)
        a = self.invoke("analyze", "--batch", self.b)
        self.assertEqual(a["analyzed"], 100)

    def test_corrupt_interrupted_retry_and_provenance(self):
        self.new()
        self.engine.mode = "interrupt"
        self.run_job(expected=40)
        v = self.invoke("verify", "--batch", self.b, expected=40)
        self.assertEqual(v["counts"]["interrupted"], 1)
        self.engine.mode = "invalid"
        self.run_job("S1_low_G0_seed0", "--resume", expected=30)
        self.engine.mode = "crash"
        self.run_job("S1_low_G0_seed0", "--resume", expected=20)
        self.engine.mode = "miss"
        result = self.run_job("S1_low_G0_seed0", "--resume")
        p = Path(result["attempt"])
        m = ex.json_read(p / "run_manifest.json")
        cfg = ex.json_read(p / "config_snapshot.json")
        self.assertEqual(cfg["data_origin"], "synthetic")
        self.assertEqual(cfg["scenario"], "S1")
        (p / "logs" / "out.csv").unlink()
        self.invoke("verify", "--batch", self.b, expected=30)
        self.run_job(expected=40)
        replacement = Path(self.run_job("S1_low_G0_seed0", "--resume")["attempt"])
        self.assertTrue(p.exists())
        m2 = ex.json_read(replacement / "run_manifest.json")
        self.assertEqual(m["simulation_job_key"], m2["simulation_job_key"])
        v = self.invoke("verify", "--batch", self.b, expected=40)
        self.assertEqual(v["attempts"], 5)
        self.assertEqual(v["counts"]["valid"], 1)
        cfg = ex.json_read(self.cfg)
        ex.save(self.cfg, dict(cfg, control={"rmax": 9}))
        self.run_job("S1_low_G1_seed0", expected=2)
        self.assertEqual(len(self.engine.calls), 5)

    def test_relocated_evidence_and_analysis_version(self):
        import shutil

        self.new()
        p = Path(self.run_job()["attempt"])
        b = ex.batch_read(self.b)
        other = self.root / "restored" / p.name
        shutil.copytree(p, other)
        state, m = ex.inspect_attempt(other, b, b["jobs"][0])
        self.assertEqual(state, "completed")
        phys = m["simulation_job_key"]
        first = self.invoke("analyze", "--batch", self.b, expected=40)
        tools = self.root / "tools"
        tools.mkdir()
        (tools / "formal_state.py").write_text("# new analysis version")
        second = self.invoke("analyze", "--batch", self.b, expected=40)
        self.assertNotEqual(first["analysis_version"], second["analysis_version"])
        cfg = ex.json_read(self.cfg)
        res_cfg = dict(cfg, resource_probe={"mode":"short"})
        scientific_sources = ex.simulation_sources(self.root)
        regular_before, probe_before = (ex.analysis_id(self.root,c) for c in (cfg,res_cfg))
        (tools / "resource_probe.py").write_text("# cut-off audit change")
        self.assertEqual(regular_before, ex.analysis_id(self.root,cfg))
        self.assertNotEqual(probe_before, ex.analysis_id(self.root,res_cfg))
        probe_before = ex.analysis_id(self.root,res_cfg)
        (tools / "terminal_coverage.py").write_text("# final-control evidence change")
        self.assertNotEqual(regular_before, ex.analysis_id(self.root,cfg))
        self.assertNotEqual(probe_before, ex.analysis_id(self.root,res_cfg))
        self.assertEqual(scientific_sources, ex.simulation_sources(self.root))
        self.run_job()
        self.assertEqual(len(self.engine.calls), 1)
        self.assertEqual(ex.json_read(p / "run_manifest.json")["simulation_job_key"], phys)

    def test_actual_fixture_and_formal_analyzer_cases(self):
        from types import SimpleNamespace
        from formal_fixtures import fixture_config, prepare_fixture_inputs, run_engine
        from formal_analyze import analyze_attempt

        cfg = fixture_config()
        for key in ("scenario", "scheme", "seed", "load_id", "data_origin"):
            cfg.pop(key, None)
        ex.save(self.cfg, cfg)
        self.new()
        cases = {
            "S0_low_G0_seed0": "prior_only",
            "S1_low_G0_seed0": "not_detected",
            "S1_low_G1_seed0": "late",
            "S1_low_G2_seed0": "missing_log",
        }

        def simulate(config, job, attempt):
            return run_engine(config, dict(job, fixture_case=cases[job["job_id"]]), attempt)

        fixture = SimpleNamespace(prepare_inputs=prepare_fixture_inputs, run_engine=simulate)
        for job, case in cases.items():
            with self.subTest(case=case), contextlib.redirect_stdout(
                io.StringIO()
            ), contextlib.redirect_stderr(io.StringIO()):
                code = ex.main(
                    ["run", "--batch", str(self.b), "--job", job],
                    engine=fixture,
                    analyzer=analyze_attempt,
                    project=self.root,
                    config_loader=ex.json_read,
                )
                self.assertEqual(code, 30 if case == "missing_log" else 0)
        verified = self.invoke("verify", "--batch", self.b, expected=30)
        self.assertEqual((verified["counts"]["valid"], verified["counts"]["invalid"]), (3, 1))

    def test_tampered_matrix_and_batch_lock(self):
        self.new()
        with ex.locked(self.b):
            self.invoke("run", "--batch", self.b, expected=40)
        (self.b / "matrix.csv").write_text("tampered")
        self.invoke("verify", "--batch", self.b, expected=30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
