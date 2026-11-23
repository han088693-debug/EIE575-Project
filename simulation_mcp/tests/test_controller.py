"""File-queue controller boundary tests; the host and process launch are mocked."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings
from controller import Controller, parse_progress, read_json, save_json, tail_text


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eie575-controller-test-", dir=r"E:\Temp")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.settings = Settings(self.base / "source", self.base / "tools", self.base / "state")
        self.controller = Controller(self.settings)
        self.ready = patch.object(Settings, "check", return_value={"ready": True, "scope": "test fixture"})
        self.ready.start()
        self.addCleanup(self.ready.stop)
        self.alive = patch("controller.process_alive", return_value=False)
        self.alive.start()
        self.addCleanup(self.alive.stop)
        # The controller now only queues work. Guard globally against accidental
        # process creation by any imported helper as the host architecture evolves.
        self.spawn = patch("subprocess.Popen", side_effect=AssertionError("Tests must not spawn a process"))
        self.mock_spawn = self.spawn.start()
        self.addCleanup(self.spawn.stop)

    def job(self, *, status="running", run_status="running", request_id=None, result=False):
        job_id = "sim_20261122T120000_012345abcdef"
        directory = self.settings.state_root / "jobs" / job_id
        directory.mkdir(parents=True, exist_ok=True)
        now = time.time() - 3
        run = {"index": 0, "scheme": "G0", "seed": 0, "status": run_status, "result_dir": None}
        if result:
            output = directory / "project" / "results" / "G0_seed0_fixture"
            output.mkdir(parents=True, exist_ok=True)
            run["result_dir"] = str(output)
        job = {"job_id": job_id, "status": status, "request_id": request_id,
               "created_epoch": now, "heartbeat_epoch": now, "schemes": ["G0"], "seeds": [0],
               "scope": "test fixture", "current_run": 0, "worker": {"pid": 99999999, "create_time": 1.0},
               "engine": None, "runs": [run]}
        save_json(directory / "job.json", job)
        return job, directory

    def write_job(self, job, directory):
        save_json(directory / "job.json", job)

    def test_invalid_schemes(self):
        for value in ([], "G0", ["G4"], ["G0", "G0"], [0], [True], ["../G0"]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.controller.start_run(schemes=value)
        self.mock_spawn.assert_not_called()

    def test_invalid_seeds(self):
        for value in ([], 0, [True], [False], [-1], [2147483648], [0.0], ["0"], [0, 0]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.controller.start_run(seeds=value)

    def test_batch_size_limit(self):
        with self.assertRaisesRegex(ValueError, "100"):
            self.controller.start_run(schemes=["G0", "G1", "G2", "G3"], seeds=list(range(26)))

    def test_invalid_request_ids(self):
        for value in ("", "a" * 65, "../request", "with space", "line\nbreak", 123, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.controller.start_run(request_id=value)

    def test_environment_not_ready(self):
        with patch.object(Settings, "check", return_value={"ready": False, "missing": ["engine.exe"]}):
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                self.controller.start_run()

    def test_job_id_paths_rejected(self):
        for value in ("../secret", r"E:\Temp\secret", "sim_20261122T120000_012345abcdef/../", "", None, 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.controller.job_dir(value)
        with self.assertRaisesRegex(ValueError, "Unknown"):
            self.controller.job_dir("sim_20261122T120000_999999999999")

    def test_log_paths_and_limits_rejected(self):
        job, _ = self.job()
        for log in ("../worker.log", r"E:\Temp\secret", "simulation.log", ""):
            with self.subTest(log=log), self.assertRaises(ValueError):
                self.controller.tail_log(job["job_id"], log=log)
        for lines in (0, -1, 501, True, "80"):
            with self.subTest(lines=lines), self.assertRaises(ValueError):
                self.controller.tail_log(job["job_id"], lines=lines)

    def test_run_indices_rejected(self):
        job, _ = self.job()
        for index in (-1, 1, True, "0"):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.controller.tail_log(job["job_id"], run_index=index)
            with self.subTest(result_index=index), self.assertRaises(ValueError):
                self.controller.get_results(job["job_id"], run_index=index)

    def test_tail_log_is_limited_and_missing_is_explicit(self):
        job, directory = self.job()
        (directory / "worker.log").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
        result = self.controller.tail_log(job["job_id"], log="worker", lines=2)
        self.assertEqual(result["text"], "three\nfour")
        self.assertTrue(result["truncated"])
        missing = self.controller.tail_log(job["job_id"], log="simulation")
        self.assertFalse(missing["available"])
        self.assertEqual(missing["text"], "")

    def test_tail_text_caps_bytes_and_discards_partial_first_line(self):
        path = self.base / "long.log"
        path.write_text("prefix-that-is-too-long\nline2\nline3\n", encoding="utf-8")
        value = tail_text(path, lines=100, byte_limit=18)
        self.assertTrue(value["truncated"])
        self.assertEqual(value["text"], "line2\nline3")
        self.assertLessEqual(len(value["text"].encode()), 18)

    def test_latest_progress_is_parsed(self):
        value = parse_progress("** Event #20 T=1.5\n** Event #99 T=1.5e1 Elapsed: 2s\n")
        self.assertEqual(value["event_number"], 99)
        self.assertEqual(value["simulation_time_s"], 15.0)
        self.assertEqual(value["percent"], 50.0)
        self.assertIsNone(parse_progress("initializing only"))
        self.assertEqual(parse_progress("Event #999 T=31")["percent"], 100.0)

    def test_real_omnetpp_lowercase_progress_format(self):
        value = parse_progress("** Event #9422   t=30   Elapsed: 0.315s (0m 00s)  100% completed\n")
        self.assertIsNotNone(value)
        self.assertEqual(value["event_number"], 9422)
        self.assertEqual(value["simulation_time_s"], 30.0)
        self.assertEqual(value["percent"], 100.0)

    def test_missing_worker_is_interrupted_without_mutating_manifest(self):
        job, directory = self.job()
        before = (directory / "job.json").read_bytes()
        value = self.controller.get_status(job["job_id"])
        self.assertEqual(value["status"], "interrupted")
        self.assertFalse(value["worker_alive"])
        self.assertIn("no longer alive", value["error"])
        self.assertEqual(before, (directory / "job.json").read_bytes())

    def test_queued_task_without_worker_stays_queued_when_host_is_ready(self):
        job, directory = self.job(status="queued", run_status="pending")
        job["worker"] = None
        self.write_job(job, directory)
        with patch.object(Controller, "host_status", return_value={"ready": True}):
            value = self.controller.get_status(job["job_id"])
        self.assertEqual(value["status"], "queued")
        self.assertFalse(value["worker_alive"])
        self.assertNotIn("error", value)
        with patch.object(Controller, "host_status", return_value={"ready": False}):
            self.assertEqual(self.controller.get_status(job["job_id"])["status"], "interrupted")

    def test_start_requires_live_host_and_does_not_create_a_job_when_absent(self):
        self.assertFalse(self.controller.host_status()["ready"])
        with self.assertRaisesRegex(RuntimeError, "host is not ready"):
            self.controller.start_run()
        self.assertEqual(self.controller._all_jobs(), [])
        self.mock_spawn.assert_not_called()

    def test_start_with_ready_host_only_enqueues_serial_batch(self):
        with patch.object(Controller, "host_status", return_value={"ready": True}):
            value = self.controller.start_run(schemes=["G0", "G3"], seeds=[1, 2])
        self.assertEqual(value["status"], "queued")
        self.assertFalse(value["worker_alive"])
        self.assertEqual([(run["scheme"], run["seed"]) for run in value["runs"]],
                         [("G0", 1), ("G3", 1), ("G0", 2), ("G3", 2)])
        self.assertTrue(all(run["status"] == "pending" for run in value["runs"]))
        self.mock_spawn.assert_not_called()

    def test_cancelled_output_is_never_complete(self):
        job, _ = self.job(status="cancelled", run_status="cancelled", result=True)
        save_json(Path(job["runs"][0]["result_dir"]) / "summary.json",
                  {"passed": True, "checks": [{"name": "example", "passed": True}]})
        value = self.controller.get_results(job["job_id"])
        self.assertFalse(value["complete"])
        self.assertFalse(value["runs"][0]["complete"])
        self.assertIsNotNone(value["runs"][0]["summary"])

    def test_successful_result_requires_readable_summary(self):
        job, _ = self.job(status="passed", run_status="passed", result=True)
        output = Path(job["runs"][0]["result_dir"])
        self.assertFalse(self.controller.get_results(job["job_id"])["complete"])
        (output / "summary.json").write_text("not json", encoding="utf-8")
        corrupt = self.controller.get_results(job["job_id"])
        self.assertFalse(corrupt["complete"])
        self.assertIn("summary_error", corrupt["runs"][0])
        save_json(output / "summary.json", {"passed": True, "checks": [{"name": "example", "passed": True}]})
        complete = self.controller.get_results(job["job_id"])
        self.assertTrue(complete["complete"])
        self.assertTrue(complete["runs"][0]["complete"])
        self.assertEqual(complete["runs"][0]["file_count"], 1)

    def test_passed_manifest_does_not_override_failed_or_missing_summary_verdict(self):
        job, _ = self.job(status="passed", run_status="passed", result=True)
        output = Path(job["runs"][0]["result_dir"])
        for verdict in (False, None, 1, "true"):
            with self.subTest(verdict=verdict):
                save_json(output / "summary.json", {"passed": verdict, "checks": []})
                for run_index in (None, 0):
                    value = self.controller.get_results(job["job_id"], run_index=run_index)
                    self.assertFalse(value["complete"])
                    self.assertFalse(value["runs"][0]["complete"])
        save_json(output / "summary.json", {"checks": []})
        self.assertFalse(self.controller.get_results(job["job_id"])["complete"])

    def test_complete_requires_nonempty_checks_with_strict_true_verdicts(self):
        job, _ = self.job(status="passed", run_status="passed", result=True)
        output = Path(job["runs"][0]["result_dir"])
        for checks in ([], [{"passed": False}], [{"passed": 1}], [{"passed": "true"}], [{}],
                       [{"passed": True}, {"passed": False}]):
            with self.subTest(checks=checks):
                save_json(output / "summary.json", {"passed": True, "checks": checks})
                value = self.controller.get_results(job["job_id"])
                self.assertFalse(value["complete"])
                self.assertFalse(value["runs"][0]["complete"])

    def test_malformed_summary_structure_returns_error_without_crashing(self):
        job, _ = self.job(status="passed", run_status="passed", result=True)
        output = Path(job["runs"][0]["result_dir"])
        for summary in ({"passed": True, "checks": "invalid"},
                        {"passed": True, "checks": {"example": True}},
                        {"passed": True, "checks": ["not-a-check"]},
                        {"passed": True, "checks": [{"passed": True}], "metrics": "invalid"},
                        ["not-an-object"]):
            with self.subTest(summary=summary):
                save_json(output / "summary.json", summary)
                for run_index in (None, 0):
                    value = self.controller.get_results(job["job_id"], run_index=run_index)
                    self.assertFalse(value["complete"])
                    self.assertIsNone(value["runs"][0]["summary"])
                    self.assertIn("Malformed", value["runs"][0]["summary_error"])

    def test_default_results_are_compact_and_explicit_run_returns_detail(self):
        job, _ = self.job(status="failed", run_status="failed", result=True)
        output = Path(job["runs"][0]["result_dir"])
        summary = {"passed": False, "scope": "test fixture", "checks": [
            {"name": "finished", "passed": True}, {"name": "deadline", "passed": False}],
            "metrics": {"events": [{"event_id": 7}], "large_detail": [1, 2, 3]}}
        save_json(output / "summary.json", summary)
        (output / "simulation.log").write_text("done\n", encoding="utf-8")
        compact = self.controller.get_results(job["job_id"])["runs"][0]
        self.assertNotIn("files", compact)
        self.assertEqual(compact["file_count"], 2)
        self.assertEqual(compact["summary"]["checks_total"], 2)
        self.assertEqual(compact["summary"]["failed_checks"], [summary["checks"][1]])
        self.assertEqual(compact["summary"]["events"], [{"event_id": 7}])
        self.assertNotIn("metrics", compact["summary"])
        detailed = self.controller.get_results(job["job_id"], run_index=0)["runs"][0]
        self.assertEqual(detailed["summary"], summary)
        self.assertEqual({item["name"] for item in detailed["files"]}, {"summary.json", "simulation.log"})
        self.assertNotIn("file_count", detailed)

    def test_result_path_cannot_escape_task(self):
        job, directory = self.job(status="passed", run_status="passed")
        outside = self.base / "outside"
        outside.mkdir()
        save_json(outside / "summary.json", {"private": True})
        job["runs"][0]["result_dir"] = str(outside)
        self.write_job(job, directory)
        with self.assertRaisesRegex(ValueError, "outside"):
            self.controller.get_results(job["job_id"])
        with self.assertRaisesRegex(ValueError, "outside"):
            self.controller.tail_log(job["job_id"])

    def test_same_request_returns_original_task_without_spawning(self):
        job, _ = self.job(status="passed", run_status="passed", request_id="stable-request")
        result = self.controller.start_run(schemes=["G0"], seeds=[0], request_id="stable-request")
        self.assertEqual(result["job_id"], job["job_id"])
        self.assertEqual(result["status"], "passed")
        self.mock_spawn.assert_not_called()

    def test_idempotent_lookup_succeeds_after_environment_and_host_become_unavailable(self):
        job, _ = self.job(status="passed", run_status="passed", request_id="stable-request")
        with patch.object(Settings, "check", return_value={"ready": False, "missing": ["engine.exe"]}), \
             patch.object(Controller, "host_status", return_value={"ready": False}):
            value = self.controller.start_run(schemes=["G0"], seeds=[0], request_id="stable-request")
        self.assertEqual(value["job_id"], job["job_id"])
        self.assertEqual(value["status"], "passed")
        self.mock_spawn.assert_not_called()

    def test_request_id_with_different_parameters_is_rejected(self):
        self.job(status="passed", run_status="passed", request_id="stable-request")
        with self.assertRaisesRegex(ValueError, "different parameters"):
            self.controller.start_run(schemes=["G3"], seeds=[0], request_id="stable-request")
        self.mock_spawn.assert_not_called()

    def test_active_task_blocks_another_start(self):
        job, _ = self.job()
        with patch("controller.process_alive", side_effect=lambda identity: identity is not None):
            with self.assertRaisesRegex(RuntimeError, job["job_id"]):
                self.controller.start_run(schemes=["G3"], seeds=[2])
        self.mock_spawn.assert_not_called()

    def test_corrupt_manifest_blocks_start(self):
        _, directory = self.job()
        (directory / "job.json").write_text("broken", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Cannot read task manifest"):
            self.controller.start_run()
        self.mock_spawn.assert_not_called()

    def test_stop_terminal_task_is_idempotent(self):
        job, directory = self.job(status="cancelled", run_status="cancelled")
        value = self.controller.stop_run(job["job_id"])
        self.assertEqual(value["status"], "cancelled")
        self.assertFalse((directory / "cancel.request").exists())

    def test_json_update_leaves_no_temporary_files(self):
        target = self.base / "status.json"
        save_json(target, {"phase": "first"})
        save_json(target, {"phase": "second", "message": "完成"})
        self.assertEqual(read_json(target), {"phase": "second", "message": "完成"})
        self.assertEqual(list(self.base.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
