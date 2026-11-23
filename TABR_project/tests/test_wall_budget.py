"""Runtime regression tests using temporary fixtures and an owned Python child, with no SUMO/OMNeT launch."""

import json
import hashlib
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "tools"))
import experiment as ex
import formal_engine as engine
import wall_budget as wb


class BudgetTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name)
        self.attempt = self.project / "attempt-test"
        self.logs = self.attempt / "logs"
        self.inputs = self.attempt / "inputs"
        self.logs.mkdir(parents=True)
        self.inputs.mkdir()
        self.tag = "a" * 64
        self.key = "b" * 64
        self.binding = dict(
            request_id="synthetic1",
            job_id="E0_candidate_G0_seed100",
            scope="engineering",
            registration_sha256=self.tag,
            physics_sha256=self.tag,
        )
        self.run = self.attempt.name
        ex.save(
            self.attempt / "run_manifest.json",
            dict(
                attempt_id=self.run,
                run_id=self.run,
                job_id=self.binding["job_id"],
                scope=self.binding["scope"],
                physics_sha256=self.tag,
                simulation_job_key=self.key,
                status="running",
            ),
        )

    def grant(self, initial=2, ceiling=8, auto=False):
        row = dict(
            self.binding,
            authorized_cap_s=ceiling,
            initial_mode="full_cap",
            initial_limit_s=initial,
            continue_on_progress=auto,
        )
        source = self.project / "approval.json"
        ex.save(source, dict(format="eie575-wall-approval-v1", approved=True, requests=[row]))
        return wb.register_grant(self.project, source, ex.file_hash(source), "synthetic1")

    def worker(self, configured=2, **options):
        return wb.Supervisor(self.project, self.attempt, configured, "synthetic1", self.tag, **options)

    def command(self, seconds=6, attempt=None, key=None):
        return wb.extend(
            self.project, "synthetic1", attempt or self.run, key or self.key, seconds, "reviewed fixture"
        )

    def ticks(self, value):
        for name, field in (("scenario_truth.csv", "t"), ("control_log.csv", "tick_time")):
            (self.logs / name).write_text(f"run_id,{field}\n{self.run},{value}\n")

    def test_commands_are_scoped_monotonic_and_audited(self):
        self.grant()
        s = self.worker()
        for n, a, k in [(3, "wrong", self.key), (3, self.run, "foreign"), (9, None, None), (2, None, None)]:
            with self.assertRaises(ValueError):
                self.command(n, a, k)
        self.command()
        with self.assertRaisesRegex(ValueError, "pending"):
            self.command(7)
        s.check(0)
        self.assertEqual((s.limit, s.revision), (6, 1))
        with self.assertRaises(ValueError):
            self.command(5)
        s.close()
        with self.assertRaisesRegex(ValueError, "active"):
            self.command(7)
        events = [
            json.loads(l)["event"] for l in (self.logs / "wall_budget.jsonl").read_text().splitlines()
        ]
        self.assertEqual(events, ["started", "operator_extension", "closed"])

    def test_soft_check_does_not_kill_and_hard_limit_remains_absolute(self):
        self.grant(initial=4, ceiling=8, auto=True)
        s = self.worker(configured=4, inspect_s=2, interval_s=1, stall_s=2)
        for ts in (0, 2, 4, 7):
            self.ticks(ts)
            s.check(ts, now=s.started + ts)
        self.assertEqual(s.limit, 8)
        with self.assertRaises(TimeoutError):
            s.check(9, now=s.started + 8)
        events = [
            json.loads(l)["event"] for l in (self.logs / "wall_budget.jsonl").read_text().splitlines()
        ]
        self.assertIn("soft_review", events)
        self.assertIn("approaching_hard_ceiling", events)
        self.assertIn("preauthorized_extension", events)

    def test_missing_evidence_is_distinct_from_buffering_and_no_progress(self):
        s = self.worker(configured=8, inspect_s=2, interval_s=1, stall_s=2)
        self.ticks(0)
        for ts, cpu in ((0, 0), (1, 1), (2, 2)):
            s.check(cpu, now=s.started + ts)
        self.assertEqual(s.health, "cpu_active_buffering_possible")
        s.check(2, now=s.started + 4)
        self.assertEqual(s.health, "no_observed_progress")
        (self.logs / "control_log.csv").unlink()
        s.check(2, now=s.started + 5)
        self.assertEqual(s.health, "evidence_unavailable")
        with self.assertRaises(FileNotFoundError):
            self.command(12)
        with self.assertRaises(TimeoutError):
            s.check(2, now=s.started + 8)

    def test_changed_approval_rejected(self):
        self.grant()
        root = wb.directory(self.project, "synthetic1")
        altered = ex.json_read(root / "grant.json")
        ex.save(root / "grant.json", dict(altered, authorized_cap_s=60))
        with self.assertRaisesRegex(ValueError, "terms"):
            self.worker()

    def test_worker_binding_cannot_be_reused(self):
        self.grant()
        s = self.worker()
        with self.assertRaisesRegex(ValueError, "already bound"):
            self.worker()
        s.close()
        with self.assertRaisesRegex(ValueError, "already bound"):
            self.worker()

    def test_mcp_propagates_registered_probe_and_formal_batch_identities(self):
        import formal_mcp as mcp

        cfg = self.project / "registered.json"
        cfg.write_bytes(b"{}")
        spec = dict(scenario="E0", load_id="candidate", scheme="G0", seed=100, density_per_lane_km=25)
        digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode() + cfg.read_bytes()).hexdigest()
        rows = [
            (
                {"mode": "probe", "probe": "fixture", "request_id": "fixtureP", "registration": digest},
                digest,
            ),
            (
                {
                    "mode": "run",
                    "batch": "fixture",
                    "job": "registered",
                    "request_id": "fixtureF",
                    "resume": True,
                },
                self.tag,
            ),
        ]
        for row, expected in rows:
            with self.subTest(row=row), patch.object(
                mcp.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"GO\n"))
            ), patch.object(mcp, "read_json", return_value=row), patch.object(
                mcp, "probe_output", return_value=self.project
            ), patch.object(
                mcp, "probe_definition", return_value=(cfg, spec)
            ), patch.object(
                mcp,
                "job_definition",
                return_value=(self.project, {"physics_sha256": self.tag}, {"job_id": "registered"}),
            ), patch.object(
                mcp.subprocess, "call", return_value=0
            ) as child:
                self.assertEqual(mcp.execute_gate(row["request_id"]), 0)
                env = child.call_args.kwargs["env"]
                self.assertEqual(
                    (env["EIE575_WALL_REQUEST"], env["EIE575_WALL_REGISTRATION"]),
                    (row["request_id"], expected),
                )

    def test_copy_on_windows_rename_denial_and_input_verification(self):
        src = self.inputs / ".cmdenv-log"
        dst = self.logs / "cmdenv-internal.log"
        (self.inputs / "formal.ini").write_text("[General]\n")
        original = ex.input_fingerprint(self.inputs)
        src.write_bytes(b"closed console bytes")
        with patch.object(Path, "replace", side_effect=PermissionError("shared")):
            self.assertEqual(engine.archive_cmdenv(src, dst)["state"], "copied_source_retained")
        self.assertEqual(ex.input_fingerprint(self.inputs), original)
        dst.write_bytes(b"other")
        with self.assertRaisesRegex(ValueError, "unverified"):
            ex.input_fingerprint(self.inputs)

    def launch_fixture(self, extend):
        if os.name != "nt":
            self.skipTest("Windows Job Object integration")
        if extend:
            self.grant(initial=3, ceiling=12)
        child = self.project / "fixture_worker.py"
        child.write_text(
            "from pathlib import Path\nimport os,sys,time\n"
            "p=Path(sys.argv[1]); (p/'start.pid').write_text(str(os.getpid()));"
            "time.sleep(3.8); (p/'finish.pid').write_text(str(os.getpid()))\n"
        )
        cfg = {
            "tools": ex.json_read(HERE / "configs/core.json")["tools"],
            "observation_mode": "registered_pairs",
            "execution": {"wall_timeout_s": 3 if extend else 1.5},
        }

        def commands(config, job, folder, port):
            (folder / "veins_runtime/sumo-launchd-tmp-test").mkdir()
            return (
                [sys.executable, "-c", "import time;time.sleep(30)"],
                [sys.executable, str(child), str(self.logs)],
                os.environ.copy(),
            )

        errors = []

        def operator():
            try:
                until = time.monotonic() + 3
                while not (self.logs / "start.pid").is_file() and time.monotonic() < until:
                    time.sleep(0.01)
                self.assertTrue((self.logs / "start.pid").is_file())
                self.command(10)
            except BaseException as exc:
                errors.append(exc)

        t = threading.Thread(target=operator)
        diag = dict(
            expected_entries=1,
            actual_entries=1,
            missing_entries=[],
            unexpected_entries=[],
            unexplained_removals=0,
            unexplained_teleports=0,
        )
        with patch.object(engine, "PROJECT", self.project), patch.object(
            engine, "process_commands", commands
        ), patch.dict(
            os.environ, EIE575_WALL_REQUEST="synthetic1", EIE575_WALL_REGISTRATION=self.tag
        ), patch.object(
            engine, "owns_listener", return_value=True
        ), patch.object(
            engine, "read_exit", return_value=0
        ), patch.object(
            engine, "actual_duration", return_value=1
        ), patch.object(
            engine, "traffic_diagnostics", return_value=diag
        ), patch.object(
            engine, "SAMPLE_S", 0.05
        ):
            if extend:
                t.start()
                try:
                    result = engine.run_engine(cfg, {}, self.attempt)
                finally:
                    t.join(5)
            else:
                with patch.object(
                    engine, "archive_cmdenv", side_effect=PermissionError("forced sharing violation")
                ):
                    with self.assertRaisesRegex(TimeoutError, "Per-job wall timeout"):
                        engine.run_engine(cfg, {}, self.attempt)
                result = ex.json_read(self.logs / "engine_status.json")
        if errors:
            raise errors[0]
        return result

    def test_actual_hard_deadline_preserves_timeout_despite_finalization_error(self):
        result = self.launch_fixture(False)
        self.assertEqual(result["status"], "interrupted_timeout")
        self.assertFalse(result["engine_complete"])
        self.assertIn("sharing violation", result["finalization_error"])
        self.assertEqual(ex.json_read(self.logs / "engine_terminal.json")["status"], "interrupted_timeout")
        self.assertFalse((self.logs / "finish.pid").exists())
        print(
            "hard deadline preserved",
            self.run,
            result["wall_timeout_s"],
            result["error"],
            result["finalization_error"],
        )

    def test_real_owned_child_has_one_pid_and_attempt_across_original_deadline(self):
        result = self.launch_fixture(True)
        self.assertTrue(result["engine_complete"])
        self.assertGreater(result["wall_seconds"], 3)
        pid = (self.logs / "start.pid").read_text()
        self.assertEqual((self.logs / "finish.pid").read_text(), pid)
        self.assertTrue(any(p["pid"] == int(pid) for p in result["pids"]))
        self.assertEqual(ex.json_read(self.logs / "wall_budget_state.json")["attempt_id"], self.run)
        self.assertEqual(result["wall_timeout_s"], 10)
        print(
            "owned OS child",
            pid,
            "attempt",
            self.run,
            "old_s",
            3,
            "effective_s",
            10,
            "elapsed_s",
            round(result["wall_seconds"], 3),
        )


if __name__ == "__main__":
    unittest.main()
