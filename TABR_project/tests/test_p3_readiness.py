"""Assert the P3 orchestration contracts with isolated fixtures and no live jobs."""

import copy
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "tools"))
import experiment as ex
import p3_readiness as gate
import p3_recovery as ck


class P3Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        (self.project / "configs").mkdir(parents=True)
        (self.project / "results").mkdir()
        self.cfg = ex.json_read(Path(ex.__file__).resolve().parents[1] / "configs/core.json")
        self.selected = {
            "selected": {"low": 5, "high": 15},
            "warmup_s": 20,
            "version": {"sources": {}, "runtime": {}},
            "e0_index_sha256": "a" * 64,
            "e0_decision_sha256": "b" * 64,
        }
        self.active = gate.active_config(self.cfg, self.selected)
        self.dst = self.root / "state"
        self.dst.mkdir()
        self.state = dict(status="engineering", selection=self.selected, requests=[])
        ex.save(self.project / "configs/formal_probes.json", {"probes": {}})
        self.rows = {}
        self.calls = []
        self.fail_once = False

    def obs(self, j, reply, v, cfg):
        token = reply["request_id"]
        p = self.root / "raw" / token
        p.mkdir(parents=True)
        (p / "input").write_text("registered physical inputs", encoding="utf-8")
        (p / "log").write_text("raw events and misses", encoding="utf-8")
        m = dict(
            status="completed",
            scope="engineering",
            data_valid=True,
            files=ex.hashes(p),
            simulation_job_key=ex.identity(j["job"]),
            attempt_id=token,
        )
        ex.save(p / "run_manifest.json", m)
        return dict(
            **j,
            attempt_id=token,
            attempt=str(p),
            manifest_sha256=ex.file_hash(p / "run_manifest.json"),
            simulation_job_key=m["simulation_job_key"],
            trajectory=ex.identity([j["scenario"], j["seed"]]),
            wall_seconds=10,
            private_bytes=500,
            handles=22,
            output_bytes=350 if j["role"] == "resource_pilot" and j["load_id"] == "low" else 100
        )

    def rpc(self, cfg, action, payload):
        rid = payload["request_id"]
        if action == "start_probe":
            name = payload["probe"]
            if rid not in self.rows:
                self.calls.append(rid)
                self.rows[rid] = name
            return dict(request_id=rid, probe=name, state="queued")
        if self.fail_once:
            state = "failed"
        else:
            state = "completed"
        return dict(request_id=rid, probe=self.rows[rid], state=state, complete=state == "completed")

    def step(self, **kwargs):
        return gate.advance(
            self.project, self.dst, self.state, self.active, "testtag", self.rpc, validate=self.obs, **kwargs
        )

    def finish(self):
        for _ in range(40):
            result, code = self.step()
            if code == 0:
                return
        self.fail("did not finish")

    def test_no_e0_no_registration_or_execution(self):
        cfg_path = self.project / "configs/core.json"
        ex.save(cfg_path, self.cfg)
        result, code = gate.run(
            cfg_path, self.root / "missing", self.dst, project=self.project, call=self.rpc
        )
        self.assertEqual(code, 40)
        self.assertEqual(result["status"], "awaiting_real_e0")
        self.assertFalse(self.calls)
        self.assertFalse((self.dst / "index.json").exists())

    def test_retry_order_and_relocated_data(self):
        self.fail_once = True
        self.assertEqual(self.step()[1], 20)
        self.assertEqual(self.step()[1], 20)
        self.assertEqual(len(self.calls), 1)
        self.fail_once = False
        self.assertEqual(self.step(retry=True)[1], 40)
        self.state = ex.json_read(self.dst / "index.json")
        self.finish()
        rows = gate.accepted(self.state, deep=True)
        self.assertEqual(len(rows), 20)
        self.assertEqual([r["scenario"] for r in rows[:12]], ["S0"] * 4 + ["S1"] * 4 + ["S2"] * 4)
        self.assertEqual({r["seed"] for r in rows[14:18]}, {300, 301})
        self.assertEqual(sum(r["role"] == "complete_heaviest_block" for r in rows), 2)
        self.assertEqual(len(self.calls), len(rows) + 1)
        (Path(rows[0]["attempt"]) / "input").write_text("truncated")
        with self.assertRaisesRegex(ValueError, "Changed block file"):
            gate.accepted(self.state, deep=True)

    def test_interruption_after_host_received_submission(self):
        original = self.rpc

        def cut(cfg, action, payload):
            value = original(cfg, action, payload)
            if action == "start_probe":
                raise RuntimeError("lost reply")
            return value

        with self.assertRaisesRegex(RuntimeError, "lost reply"):
            gate.advance(self.project, self.dst, self.state, self.active, "testtag", cut, validate=self.obs)
        self.state = ex.json_read(self.dst / "index.json")
        self.assertEqual(self.state["requests"][-1]["status"], "submitting")
        self.step()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.state["requests"][-1]["status"], "accepted")

    def test_windows_owned_path_length_preflight(self):
        j = gate.job("S2", "high", "G3", 200, "E1_high")
        ids = []
        for request_number in (1, 1, 2):
            req, longest = gate.native_path_budget(
                Path("E:/Temp/eie575_tabr"), "ab" * 7, j, request_number, self.active
            )
            ids.append(req)
            self.assertLessEqual(len(longest.encode("utf-16-le")) // 2, 259)
        self.assertEqual(ids[0], ids[1])
        self.assertNotEqual(ids[0], ids[2])
        self.assertLess(len(gate.probe_name("ab" * 7, j)), 13)
        with self.assertRaisesRegex(ValueError, "exceeds 259"):
            gate.native_path_budget(self.project / ("x" * 120), "ab" * 7, j, 1, self.active)

    def test_stratum_timeout_and_binding(self):
        import p3_execution as policy

        limits = {"S0_low": 70, "S0_high": 140, "S1_low": 75, "S1_high": 135, "S2_high": 210}
        active = dict(self.active, execution={"wall_timeout_by_stratum_s": limits, "wall_timeout_s": 210})
        self.assertEqual(
            policy.effective_policy(active, {"scenario": "S1", "load_id": "low"})["wall_timeout_s"], 75
        )
        self.assertEqual(active["execution"]["wall_timeout_s"], 210)
        record = {
            "execution_policy_sha256": ex.identity(active["execution"]),
            "simulation_sources": {},
            "runtime_hashes": {},
        }
        with patch("experiment.simulation_sources", return_value={}), patch(
            "experiment.runtime_hashes", return_value={}
        ):
            policy.verify_policy(active, record, self.project)
            with self.assertRaisesRegex(ValueError, "policy differs"):
                policy.verify_policy(self.active, record, self.project)
            with self.assertRaisesRegex(ValueError, "runtime differs"):
                policy.verify_policy(active, dict(record, runtime_hashes={"x": "changed"}), self.project)

    def test_watch_reads_existing_e0_before_starting_anything(self):
        from contextlib import redirect_stdout
        from io import StringIO

        output = self.root / "e0"
        index = output / "index.json"
        ex.save(index, {"status": "initial_runs_required", "requests": []})
        polls = []

        def fake_sleep(seconds):
            polls.append(seconds)
            ex.save(index, {"status": "selected", "requests": []})

        with redirect_stdout(StringIO()):
            self.assertIsNone(gate.wait_for_e0(output, 2, sleep=fake_sleep))
        self.assertEqual(polls, [2])
        self.assertEqual(self.calls, [])
        for terminal, why, code in (
            ("grid_exhausted", None, 40),
            ("candidate_runs_required", "invalid_measurement", 30),
        ):
            ex.save(index, {"status": terminal, "requests": [{"state": why}]})
            result, actual = gate.wait_for_e0(output, 2, sleep=lambda _: self.fail("unexpected wait"))
            self.assertEqual(actual, code)
            self.assertFalse(result["submitted"])

    def test_snapshot_selects_each_frozen_budget(self):
        sources = {"S0_low": 71, "S0_high": 141, "S1_low": 77, "S1_high": 133, "S2_high": 214}
        from experiment import job_snapshot

        cfg = copy.deepcopy(self.active)
        candidate = job_snapshot(
            cfg, {"scenario": "S0", "load_id": "high", "scheme": "G1", "seed": 200}, "engineering"
        )
        self.assertNotIn("execution", candidate)
        cfg.update(stage="frozen", execution={"wall_timeout_by_stratum_s": sources, "wall_timeout_s": 214})
        for sc, loads in ex.SCENARIO_LOADS:
            for ld in loads:
                snap = job_snapshot(cfg, {"scenario": sc, "load_id": ld, "scheme": "G0", "seed": 0}, "formal")
                self.assertEqual(snap["execution"]["wall_timeout_s"], sources[sc + "_" + ld])
        self.assertEqual(cfg["execution"]["wall_timeout_s"], 214)

    def test_pack_retrieve_restore_then_freeze(self):
        self.finish()
        rows = gate.accepted(self.state)
        group = gate.block_for_heaviest(rows)
        winners = [r for r in rows if (r["scenario"], r["load_id"], r["seed"]) == group]
        idx_path = ck.pack_block(
            self.project,
            self.dst / "packages",
            winners,
            self.selected["version"],
            self.active,
            max_part_bytes=1200,
        )
        idx = ex.json_read(idx_path)
        self.assertGreater(len(idx["parts"]), 1)
        received = self.root / "download"
        received.mkdir()
        receipt = {"block_key": idx["block_key"], "parts": []}
        for i, part in enumerate(idx["parts"]):
            origin = idx_path.parent / part["name"]
            dest = received / part["name"]
            shutil.copy2(origin, dest)
            receipt["parts"].append(
                dict(
                    name=part["name"], library_file_id="libfile_test_" + str(i), downloaded_path=part["name"]
                )
            )
        rp = received / "receipt.json"
        ex.save(rp, receipt)
        where = self.root / "fresh"
        # Missing/corrupted parts and a 'restore' from the freshly produced original cannot pass.
        bad = copy.deepcopy(receipt)
        bad["parts"][0]["downloaded_path"] = str(idx_path.parent / idx["parts"][0]["name"])
        ex.save(rp, bad)
        with self.assertRaisesRegex(ValueError, "independently retrieved"):
            ck.recovery(idx_path, rp, where)
        ex.save(rp, receipt)
        out = ck.recovery(idx_path, rp, where)
        self.assertEqual(out["members"], 4)
        result, code = gate.freeze(self.project, self.dst, self.state, self.active, idx, out, free=1)
        self.assertEqual((result["status"], code), ("insufficient_free_space", 40))
        self.assertFalse((self.project / "configs/frozen_core.json").exists())
        with patch("p3_readiness.ex.runtime_hashes", return_value={}), patch(
            "p3_readiness.ex.simulation_sources", return_value={}
        ):
            result, code = gate.freeze(self.project, self.dst, self.state, self.active, idx, out, free=10**9)
        self.assertEqual((result["status"], code), ("frozen", 0))
        frozen = ex.json_read(Path(result["config"]))
        with patch("experiment.runtime_hashes", return_value={}), patch(
            "experiment.simulation_sources", return_value={}
        ):
            ex.check_gate(frozen, Path(result["config"]))
        self.assertEqual(
            set(frozen["execution"]["wall_timeout_by_stratum_s"]),
            {"S0_low", "S0_high", "S1_low", "S1_high", "S2_high"},
        )
        self.assertEqual(frozen["execution"]["wall_timeout_by_stratum_s"]["S1_high"], 90)


if __name__ == "__main__":
    unittest.main(verbosity=2)
