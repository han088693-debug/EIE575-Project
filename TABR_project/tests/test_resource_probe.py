"""Synthetic unit checks for resource evidence; these fixtures never release an actual scale."""
from decimal import Decimal
import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "tools"))
import resource_probe as audit


class ResourceEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        (self.root / "inputs").mkdir()
        self.fill()

    def fill(self):
        (self.logs / "engine_status.json").write_text(json.dumps(
            {"engine_complete": True, "return_code": 0, "pids": [321], "wall_seconds": 2,
             "peak_private_bytes": 202, "peak_handles": 19}), encoding="utf-8")
        (self.logs / "resource_trace.csv").write_text(
            "t_wall_s,private_bytes,handles,process_count\n0,100,11,1\n0.2,202,19,3\n", encoding="utf-8")
        (self.logs / "engine_counters.csv").write_text(
            "t_sim_s,active_vehicle_count,open_log_streams,peak_open_log_streams\n"
            "0,0,3,3\n0.1,3,10,10\n4,0,2,10\n", encoding="utf-8")

    def test_live_process_and_stream_evidence(self):
        info = audit.engine_evidence(self.root, "test", Decimal(4))
        self.assertEqual(info["peak_active_vehicle_callbacks"], 3)
        self.assertEqual(info["exact_peak_project_log_streams"], 10)
        self.assertEqual(info["sampled_peak_private_bytes"], 202)
        self.assertGreater(info["log_output_bytes"], 0)
        with (self.logs / "engine_counters.csv").open("a") as f:
            f.write("4,0,5,35\n")
        with self.assertRaisesRegex(ValueError, "high-water"):
            audit.engine_evidence(self.root, "test", Decimal(4))
        self.fill()
        (self.logs / "resource_trace.csv").write_text("t_wall_s,private_bytes,handles,process_count\n0,0,0,0\n")
        with self.assertRaisesRegex(ValueError, "No live"):
            audit.engine_evidence(self.root, "test", Decimal(4))

    def test_incomplete_or_changed_evidence_and_empty_ladder(self):
        manifest = {"status": "completed", "data_valid": True}
        (self.root / "metrics.json").write_text("{}")
        manifest["files"] = {n: audit.sha(self.root / n) for n in ("metrics.json", "logs/engine_status.json")}
        audit.finish_check(self.root, manifest)
        (self.logs / "engine_status.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            audit.finish_check(self.root, manifest)
        progress = audit.ladder([], audit.SCALES)
        self.assertEqual(progress["status"], "incomplete")
        self.assertEqual(progress["remaining_scales"], list(audit.SCALES))

    def test_road_and_radio_inputs_independently_match(self):
        folder = self.root / "inputs"
        (folder / "traffic.rou.xml").write_text('<routes><vehicle id="x"/></routes>')
        ini = folder / "formal.ini"
        prefix = "*.node[*].appl.formalFullLog = "
        ini.write_text(prefix + "true\n*.x=10\n")
        full = audit.comparable_inputs(self.root, {})
        ini.write_text(prefix + "false\n*.x=10\n")
        self.assertEqual(audit.comparable_inputs(self.root, {}), full)
        ini.write_text(prefix + "false\n*.x=20\n")
        self.assertNotEqual(audit.comparable_inputs(self.root, {}), full)

    def test_scientific_projection_keeps_physical_samples_and_fails_changed_metrics(self):
        metrics = {"run_id": "test", "transport": {"observation_mode": "full", "logged_tx": 3, "logged_rx": 6,
                                                  "all_rx": 6, "all_tx": 3},
                   "state": {"busy_audit": {"audited_windows": 90}, "pairs": {"p1": {"mean_age": .7}}},
                   "events": [{"pair_id": "p1", "delivered": False}]}
        full = audit.scientific(metrics)
        metrics["run_id"] = "test2"
        metrics["transport"].update(observation_mode="registered_pairs", logged_rx=1)
        metrics["state"]["busy_audit"]["audited_windows"] = 30
        self.assertEqual(full, audit.scientific(metrics))
        metrics["state"]["pairs"]["p1"]["mean_age"] += .01
        self.assertNotEqual(full, audit.scientific(metrics))

    def test_short_grid_rejects_a_truncated_state_or_control_stream(self):
        from formal_fixtures import fixture_config, write_fixture
        path = self.root / "short"
        cfg = fixture_config("E0")
        write_fixture(path, cfg=cfg)
        manifest = audit.doc(path / "run_manifest.json")
        end = Decimal(17)
        life, _, _, _ = audit.vehicle_counts(path, manifest["run_id"], end)
        # A short-run terminal exposure must be accounted for all the way to the stop.
        with self.assertRaisesRegex(ValueError, "Truncated terminal control"):
            audit.audit_short(path, cfg, manifest, end, life)

        ctl = path / "logs/control_log.csv"
        with ctl.open() as stream:
            terminal = {r["vehicle_id"]: r for r in csv.DictReader(stream)}
        with ctl.open("a", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(next(iter(terminal.values()))))
            for r in terminal.values():
                r.update(window_start="16.9", tick_time="17", window_end="17")
                writer.writerow(r)
        self.assertEqual(audit.audit_short(path, cfg, manifest, end, life)["fixed_control_windows"], 680)
        state = path / "logs/state_log.csv"
        lines = state.read_text().splitlines()
        state.write_text("\n".join(lines[:-1]) + "\n")
        with self.assertRaisesRegex(ValueError, "Truncated short-run registered state"):
            audit.audit_short(path, cfg, manifest, end, life)

    def test_final_tick_exception_requires_matching_independent_normal_arrival(self):
        path = self.root / "trips.xml"
        path.write_text('<tripinfos><tripinfo id="leaving" arrival="3.90" vaporized="false"/>'
                        '<tripinfo id="resident" arrival="-1"/>'
                        '<tripinfo id="bad" arrival="3.90" vaporized="true"/></tripinfos>')
        arrivals = audit.completed_trips(self.root, {"raw_evidence_paths": {"trips.xml": "trips.xml"}})
        d, step, eps = Decimal("4"), Decimal(".1"), Decimal("1e-12")
        self.assertTrue(audit.check_final_control("leaving", d, d-step, step, step, eps, arrivals))
        self.assertFalse(audit.check_final_control("resident", d, d, step, step, eps, arrivals))
        for name, last in [("resident", d-step), ("bad", d-step), ("leaving", d-2*step)]:
            with self.assertRaisesRegex(ValueError, "Truncated"):
                audit.check_final_control(name, d, last, step, step, eps, arrivals)
        with self.assertRaisesRegex(ValueError, "Truncated"):
            audit.check_final_control("leaving", d-step, d-2*step, step, step, eps, arrivals)


if __name__ == "__main__":
    unittest.main()
