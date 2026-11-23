"""E0 synthetic engineering goldens; no simulator or formal observations are used."""
import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from calibrate import CbrPopulations, decide, design_key, evaluate


def config():
    cfg = json.loads((ROOT / "configs/core.json").read_text(encoding="utf-8-sig"))
    cfg.update(scenario="E0", scheme="G0", seed=100, density_per_lane_km=5, data_origin="synthetic")
    return cfg


def table(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(fields.split(","))
        writer.writerows(rows)


def write_e0(root, raw=lambda t: .5, smooth=lambda t: .5, rate=lambda t: 9.5,
             local=lambda t: 20, gap=False, bad_mac=False):
    cfg = config()
    targets = ("lead", "follow")
    inp, logs = root / "inputs", root / "logs"
    inp.mkdir(exist_ok=True)
    logs.mkdir(exist_ok=True)
    (root / "config_snapshot.json").write_text(json.dumps(cfg), encoding="utf-8")
    table(inp / "events.csv", "planned_event_id", [])
    monitored = [f"bg{i}" for i in range(cfg["geometry"]["background_pair_count"])]
    table(inp / "pairs.csv", "pair_id,role,sender_id,receiver_id",
          [("target", "target", *targets)] + [(car, "background", "src_" + car, car) for car in monitored])
    table(logs / "lifecycle.csv", "vehicle_id,enter_s,exit_s,busy_audited", [(c, 0, 120, 1) for c in targets])
    diag = {"expected_entries": 200, "actual_entries": 200, "insertion_failures": 0,
            "unexplained_teleports": 0, "unexplained_removals": 0}
    (logs / "sumo_diagnostics.json").write_text(json.dumps(diag), encoding="utf-8")
    control, supply, busy = [], [], [(c, 0, 0) for c in targets]
    for i in range(1200):
        start, end = i / 10, (i + 1) / 10
        r = raw(start)
        for car in targets:
            if not (gap and i == 201 and car == "follow"):
                control.append((car, start, end, end, r, smooth(start), 4, rate(start), r / 10, 3400))
            supply.append((car, start, local(start)))
            busy.extend(((car, start + .01, 1),
                         (car, start + .01 + r / 10 + (.00001 if bad_mac and i == 800 else 0), 0)))
        if 10 <= start < 110:
            for car in monitored:
                control.append((car, start, end, end, .2, .2, 3, 3, .02, 6000))
    table(logs / "control_log.csv", "vehicle_id,window_start,window_end,tick_time,C,Cbar,b,r,busy_duration_s,lane_position_m", control)
    table(logs / "local_supply.csv", "vehicle_id,t,background_count", supply)
    table(logs / "busy_audit.csv", "vehicle_id,transition_time,busy_state", busy)


class E0Measurements(unittest.TestCase):
    def test_edge_endpoints_exposure_and_vehicle_equal_weight(self):
        stat = CbrPopulations(config(), ["lead", "follow"], ["bg"], [10, 15, 20])
        stat.add("bg", 9.9, 20, .2, 1000)
        stat.add("other", 14, 16, .8, 7000)
        stat.add("interior", 14, 15, .25, 6999.99)
        stat.add("lead", 14, 15, .6, 200)
        results = stat.summary()["populations"]
        self.assertEqual(results["background_at_road_edges"][0]["exposed_vehicles"], 2)
        self.assertAlmostEqual(results["background_at_road_edges"][0]["vehicle_equal_mean_cbr"], .5)
        self.assertAlmostEqual(results["background_at_road_edges"][0]["exposure_weighted_mean_cbr"], .3)
        self.assertEqual(results["all_vehicles"][0]["exposed_vehicles"], 4)
        self.assertEqual(results["target"][1]["vehicle_equal_mean_cbr"], None)
        self.assertEqual(results["registered_background_receivers"][0]["observed_vehicle_seconds"], 5)

    def test_thresholds_saturation_and_quarter_exposure(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write_e0(root, rate=lambda t: 9.5 if 20 <= t < 42.5 else 10)
            outcome = evaluate(root)
            self.assertTrue(outcome["high"])
            self.assertFalse(outcome["low"])
            self.assertEqual(outcome["targets"]["lead"]["feedback_exposure_fraction"], .25)
            write_e0(root, raw=lambda t: .3, smooth=lambda t: .3, rate=lambda t: 10)
            self.assertTrue(evaluate(root)["low"])
            write_e0(root, raw=lambda t: .65, smooth=lambda t: .65, rate=lambda t: 10)
            outcome = evaluate(root)
            self.assertTrue(outcome["stable"])
            self.assertFalse(outcome["high"])
            self.assertIn("lead:feedback_exposure", outcome["high_failures"])

    def test_drift_oscillation_and_local_supply(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write_e0(root, raw=lambda t: .55 if t < 15 else (.62 if t < 20 else .51),
                     smooth=lambda t: .59 if int(t * 10) % 2 else .47,
                     local=lambda t: 17 if t < 20 else 10)
            outcome = evaluate(root)
            self.assertFalse(outcome["stable"])
            for reason in ("lead:pre_drift", "follow:win3:post_drift", "lead:win5:supply_drift",
                           "follow:win2:oscillation"):
                self.assertIn(reason, outcome["stability_failures"])

    def test_missing_tick_broken_mac_and_invalid_traffic(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write_e0(root, gap=True)
            with self.assertRaisesRegex(ValueError, "missing control"):
                evaluate(root)
            write_e0(root, bad_mac=True)
            with self.assertRaisesRegex(ValueError, "busy/CBR audit"):
                evaluate(root)
            diag = root / "logs/sumo_diagnostics.json"
            data = json.loads(diag.read_text())
            data["actual_entries"] -= 1
            diag.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "entries do not reconcile"):
                evaluate(root)


class E0Decision(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.runs = []

    def add(self, density, warm, labels):
        for seed, label in zip((100, 101), labels):
            self.runs.append({"density": density, "warmup_s": warm, "seed": seed,
                              "origin": "synthetic", "design_sha256": design_key(self.cfg), "valid": True,
                              "stable": label != "unstable", "high": label == "high", "low": label == "low",
                              "stability_failures": ["unstable"] if label == "unstable" else [],
                              "low_failures": [] if label == "low" else ["raw_cbr"],
                              "high_failures": [] if label == "high" else ["raw_cbr"]})

    def decision(self):
        return decide(self.cfg, self.runs, "synthetic")

    def test_initial_six_and_lowest_eligible(self):
        self.assertEqual(len(self.decision()["next_runs"]), 6)
        self.add(5, 20, ["low", "low"])
        self.add(15, 20, ["high", "high"])
        self.assertEqual(len(self.decision()["next_runs"]), 2)
        self.add(30, 20, ["high", "high"])
        report = self.decision()
        self.assertEqual(report["selected"], {"low": 5, "high": 15})
        self.assertFalse(report["eligible_for_e1"])
        with self.assertRaisesRegex(ValueError, "Mixed data"):
            decide(self.cfg, self.runs)

    def test_disagreeing_seeds_continues_fixed_order(self):
        self.add(5, 20, ["low", "none"])
        self.add(15, 20, ["none", "none"])
        self.add(30, 20, ["high", "high"])
        self.assertEqual([r["density_per_lane_km"] for r in self.decision()["next_runs"]], [1, 1])
        self.add(1, 20, ["low", "low"])
        self.assertEqual(self.decision()["selected"], {"low": 1, "high": 30})

    def test_warmup_and_independent_common_recheck(self):
        self.add(5, 20, ["low", "low"])
        self.add(15, 20, ["none", "unstable"])
        self.add(30, 20, ["none", "none"])
        self.assertEqual(self.decision()["next_runs"][0]["warmup_s"], 40)
        self.add(15, 40, ["unstable", "high"])
        self.assertEqual(self.decision()["next_runs"][0]["warmup_s"], 60)
        self.add(15, 60, ["high", "high"])
        decision = self.decision()
        self.assertEqual(decision["status"], "common_warmup_required")
        self.assertEqual([(r["density_per_lane_km"], r["warmup_s"]) for r in decision["next_runs"]], [(5, 60)] * 2)
        self.add(5, 60, ["low", "none"])
        self.assertEqual(self.decision()["status"], "common_warmup_failed")
        self.runs.pop()
        self.runs.append(dict(self.runs[-1], seed=101))
        self.assertEqual(self.decision()["status"], "selected")

    def test_infeasible_grid_48_and_reject_duplicate(self):
        for d in [5, 15, 30, 1, 2, 10, 20, 25]:
            for w in [20, 40, 60]:
                self.add(d, w, ["unstable", "unstable"])
        report = self.decision()
        self.assertEqual(report["status"], "grid_exhausted")
        self.assertEqual(report["completed_unique_runs"], 48)
        self.assertEqual(report["selected"], {"low": None, "high": None})
        self.runs.append(self.runs[0])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.decision()

    def test_sparse_preregistration_requires_both_seeds_and_replays_geometry(self):
        from formal_scenario import ScenarioGeometryError, registrations
        for density in [5, 15, 30]:
            self.add(density, 20, ["none", "none"])
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for density in [1, 2]:
                for seed in [100, 101]:
                    with self.assertRaises(ScenarioGeometryError) as exc:
                        registrations(dict(self.cfg, synthetic=True), {"scenario": "E0", "scheme": "G0",
                                      "seed": seed, "density_per_km_lane": density})
                    p = Path(d) / f"{density}_{seed}.json"
                    p.write_text(json.dumps(exc.exception.report))
                    paths.append(p)
            partial = decide(self.cfg, self.runs, "synthetic", paths[:1])
            self.assertEqual(partial["status"], "geometry_evidence_required")
            self.assertEqual(partial["missing_preregistration_seeds"], [101])
            complete = decide(self.cfg, self.runs, "synthetic", paths)
            self.assertEqual(complete["completed_unique_runs"], 6)
            self.assertEqual(complete["next_runs"][0]["density_per_lane_km"], 10)
            self.assertTrue(all(r["used_in_search"] for r in complete["geometry_exclusions"]))
            self.assertTrue(all(r["measured_cbr"] is None for r in complete["geometry_exclusions"]))
            damaged = json.loads(paths[0].read_text())
            damaged["filtering"]["excluded_distance"] += 1
            paths[0].write_text(json.dumps(damaged))
            with self.assertRaisesRegex(ValueError, "inconsistent"):
                decide(self.cfg, self.runs, "synthetic", paths)
            with self.assertRaisesRegex(ValueError, "does not establish"):
                decide(self.cfg, [], "simulation", paths)


if __name__ == "__main__":
    unittest.main()
