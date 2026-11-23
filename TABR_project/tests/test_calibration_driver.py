"""Isolated fake MCP transport. Nothing in these tests calls a simulator or the user host."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "tools"))
import calibration_driver as driver
import experiment


class FakeMcp:
    def __init__(self):
        self.calls = []
        self.requests = {}
        self.reply_lost = False
        self.final = None

    def __call__(self, cfg, tool, payload):
        self.calls.append((tool, copy.deepcopy(payload)))
        token = payload["request_id"]
        if tool == "start_probe":
            entry = self.requests.setdefault(token, {"request_id": token, "probe": payload["probe"], "state": "queued"})
            if self.reply_lost:
                self.reply_lost = False
                raise RuntimeError("Fake client disconnected after acceptance")
            return entry
        entry = self.requests[token]
        if tool == "get_status":
            return entry
        if tool == "get_results":
            return dict(entry, **(self.final or {"complete": False, "attempt_error": "per-job timeout"}))
        raise AssertionError(tool)


class CalibrationDriverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg_path = PROJECT / "configs/core.json"
        self.out = self.root / "campaign"
        self.registry = self.root / "configs/formal_probes.json"
        experiment.save(self.registry, {"probes": {"p2_existing": {"untouched": True}}})
        self.exe = self.root / driver.EXE
        self.exe.write_bytes(b"synthetic unit-test fixture. not an executable.")
        self.p2 = self.root / "verification/p2/acceptance.json"
        self.mcp = FakeMcp()

    def step(self, **kwargs):
        return driver.run(self.cfg_path, self.out, advance_once=True, project=self.root, call=self.mcp, **kwargs)

    def accept_gate(self):
        experiment.save(self.p2, {"phase": "P2", "passed": True, "executable_sha256": experiment.file_hash(self.exe)})

    def test_gate_and_read_only_peek(self):
        self.assertEqual(self.step()[0]["status"], "p2_not_accepted")
        self.assertFalse(self.out.exists())
        self.assertFalse(self.mcp.calls)
        self.accept_gate()
        self.assertEqual(self.step(status_only=True)[0]["status"], "ready")
        self.assertFalse(self.out.exists())
        self.exe.write_bytes(b"changed")
        self.assertEqual(self.step()[0]["status"], "p2_not_accepted")
        self.assertFalse(self.mcp.calls)

    def test_lost_mcp_reply_is_deduplicated_retry_keeps_failure(self):
        self.accept_gate()
        self.mcp.reply_lost = True
        with self.assertRaisesRegex(RuntimeError, "disconnected"):
            self.step()
        first = next(iter(self.mcp.requests))
        self.assertEqual(self.step()[0]["status"], "queued")
        self.assertEqual(len(self.mcp.requests), 1)
        self.mcp.requests[first]["state"] = "failed"
        error, code = self.step()
        self.assertEqual(code, 20)
        self.assertIn("timeout", error["current"]["reason"])
        self.assertEqual(self.step()[1], 20)
        started, code = self.step(retry_failed=True)
        self.assertEqual(code, 40)
        self.assertNotEqual(first, started["current"]["request_id"])
        self.assertEqual(started["current"]["job"], [5, 20, 100])
        self.assertEqual(len(self.mcp.requests), 2)
        registry = experiment.json_read(self.registry)["probes"]
        self.assertEqual(registry["p2_existing"], {"untouched": True})
        self.assertEqual(len([name for name in registry if name.startswith("p3e0_")]), 48)
        self.assertTrue(all(x["load_id"] == "candidate" for k, x in registry.items() if k.startswith("p3e0_")))

    def test_completed_synthetic_receipt_never_enters_calibration(self):
        self.accept_gate()
        first = self.step()[0]["current"]
        token = first["request_id"]
        self.mcp.requests[token]["state"] = "completed"
        attempt = self.root / "fake_attempt"
        sources = experiment.simulation_sources(self.root)
        experiment.save(attempt / "run_manifest.json", {"scope": "synthetic", "status": "completed",
            "data_valid": True, "sources": sources, "attempt_id": "test", "simulation_job_key": "test"})
        self.mcp.final = {"complete": True, "attempt": str(attempt), "attempt_id": "test", "simulation_job_key": "test"}
        result, code = self.step()
        self.assertEqual((result["status"], code), ("invalid_measurement", 30))
        self.assertFalse((self.out / "measurements").exists())

    def test_longest_grid_job_paths_fit_windows_before_engine_submission(self):
        self.accept_gate()
        receipt = self.step()[0]["current"]
        self.assertRegex(receipt["request_id"], r"^e[0-9a-f]{14}a01$")
        # Match the longest supported grid name, normal attempt token and raw SUMO log names.
        probe = "p3e0_" + "f" * 14 + "_d30_w60_s101"
        attempt = "attempt-20261122T074016391277Z-" + "a" * 12
        base = Path("E:/Temp/eie575_tabr/results/probes") / probe / receipt["request_id"]
        root = base / "results/engineering/tabr_core_v1/E0/candidate/G0/seed101" / attempt
        for suffix in (".config_snapshot.json." + "f" * 32 + ".tmp", ".run_manifest.json." + "f" * 32 + ".tmp",
                       "veins_runtime/sumo-launchd-tmp-12345678/sumo-launchd.err.log"):
            self.assertLess(len(str(root / suffix)), 260)

    def test_accepted_mock_measurement_selects_next_seed(self):
        self.accept_gate()
        first = self.step()[0]["current"]
        token = first["request_id"]
        self.mcp.requests[token]["state"] = "completed"
        self.mcp.final = {"complete": True, "attempt": str(self.root / "test-only"), "attempt_id": "t", "simulation_job_key": "t"}
        cfg = experiment.json_read(self.cfg_path)
        fake = dict(density=5, seed=100, warmup_s=20, origin="simulation", design_sha256=driver.calibrate.design_key(cfg),
                    valid=True, stable=True, low=True, high=False, stability_failures=[], low_failures=[], high_failures=["raw_cbr"],
                    test_only=True)
        with patch.object(driver, "verify_attempt", return_value="test_checksum"), patch.object(driver.calibrate, "evaluate", return_value=fake):
            nxt, code = self.step()
        self.assertEqual(code, 40)
        self.assertEqual(nxt["completed_unique_runs"], 1)
        self.assertEqual(nxt["current"]["job"], [5, 20, 101])
        self.assertEqual(len(list((self.out / "measurements").glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
