"""Checks for the per-attempt engine; never starts a live simulation."""

import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from xml.etree import ElementTree as ET

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "tools"))
import formal_engine as fe


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.cfg = json.loads((PROJECT / "configs/core.json").read_text(encoding="utf-8-sig"))
        self.job = {"scenario": "E0", "scheme": "G1", "load_id": "high", "seed": 200}
        self.raw = self.path / "raw"
        self.raw.mkdir()
        (self.path / "inputs").mkdir()

    def test_prepared_model_scope_and_real_settings(self):
        self.cfg.update(
            resource_probe=dict(background_vehicles=16, duration_s=4.0, logging_mode="registered_pairs")
        )
        fe.prepare_inputs(self.cfg, self.job, self.path / "inputs")
        i = self.path / "inputs"
        ini = (i / "formal.ini").read_text()
        for text in (
            "sim-time-limit = 4s",
            "*.manager.margin = 0",
            "hostPositionOffset = 0m",
            "formalFullLog = false",
            'formalScenario = "E0"',
            "formalSeed = 200",
            "formalGrid = 0.1s",
            "mac1609_4.rng-0 = 1",
        ):
            self.assertIn(text, ini)
        self.assertEqual(ET.parse(i / "traffic.sumo.cfg").find("time/end").get("value"), "4")
        self.assertEqual((i / "radio.xml").read_bytes(), (PROJECT / "radio.xml").read_bytes())
        self.assertTrue((i / "phases.csv").is_file())
        # The native parser rejects global-only options in [Config Formal], even in query mode.
        _, _, env = fe.runtime(self.cfg["tools"])
        q = subprocess.run(
            [
                str(PROJECT / "EIE575_TABR_Starter.exe"),
                "-u",
                "Cmdenv",
                "-q",
                "numruns",
                "-f",
                "formal.ini",
                "-c",
                "Formal",
            ],
            cwd=i,
            env=env,
            capture_output=True,
        )
        self.assertEqual(q.returncode, 0, q.stderr.decode("utf-8", "replace"))
        self.assertIn(b"Number of runs: 1", q.stdout)

    def write_traffic(self, vehicle_ids, trips, last, warnings=""):
        root = ET.Element("routes")
        for vid, when in vehicle_ids:
            ET.SubElement(root, "vehicle", id=vid, depart=str(when))
        ET.ElementTree(root).write(self.path / "inputs/traffic.rou.xml")
        root = ET.Element("tripinfos")
        for v, dep, arr, reason in trips:
            ET.SubElement(root, "tripinfo", id=v, depart=str(dep), arrival=str(arr), vaporized=reason)
        ET.ElementTree(root).write(self.raw / "trips.xml")
        root = ET.Element("summary")
        ET.SubElement(root, "step", **{k: str(v) for k, v in last.items()})
        ET.ElementTree(root).write(self.raw / "summary.xml")
        (self.raw / "sumo-launchd.err.log").write_text(warnings)

    def test_entries_endpoints_warnings_and_arrivals(self):
        self.write_traffic(
            [("departed", 0), ("arrived", 0), ("missing", 3), ("later", 4)],
            [("departed", 0, -1, ""), ("arrived", 0, 2, "")],
            dict(inserted=2, running=1, ended=1),
            "Warning: Teleporting vehicle 'a'\\nWarning: collision between 2 vehicles gap=-0.5\\n".replace(
                "\\n", "\n"
            ),
        )
        d = fe.traffic_diagnostics(self.path, self.raw, 4.0)
        self.assertEqual((d["expected_entries"], d["actual_entries"], d["insertion_failures"]), (3, 2, 1))
        self.assertEqual(
            (d["normal_arrivals"], d["unexplained_removals"], d["unexplained_teleports"]), (1, 0, 1)
        )
        self.assertEqual(d["negative_gap_warnings"], 1)

    def test_corrupt_summary_stops_analysis(self):
        self.write_traffic([("a", 0)], [("a", 0, -1, "")], dict(inserted=2, running=1, ended=0))
        with self.assertRaisesRegex(ValueError, "summary inserted"):
            fe.traffic_diagnostics(self.path, self.raw, 3.0)

    def test_reader_ignores_half_written_counter(self):
        p = self.path / "counters.csv"
        p.write_bytes(b"t_sim_s,active_vehicle_count,open_log_streams,peak_open_log_streams\n1,3,4,5\n2,")
        r = fe.CounterReader(p)
        self.assertEqual(r.sample()["active_vehicle_count"], "3")
        with p.open("ab") as handle:
            handle.write(b"4,4,5\n")
        self.assertEqual(r.sample()["active_vehicle_count"], "4")

    def test_launcher_receipt_and_command_output(self):
        p = self.path / "launchd.log"
        p.write_text("Result: <exit-code>0</exit-code>")
        self.assertEqual(fe.read_exit(p), 0)
        (self.path / "run_manifest.json").write_text('{"run_id":"r1"}')
        launch, sim, _ = fe.process_commands(self.cfg, self.job, self.path, 6234)
        self.assertIn("-t", launch)
        self.assertIn("--*.manager.port=6234", sim)
        self.assertIn('--*.brakeScenario.runId="r1"', sim)
        self.assertIn("formal.ini", sim)


if __name__ == "__main__":
    unittest.main(verbosity=2)
