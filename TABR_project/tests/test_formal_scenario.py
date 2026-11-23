"""Preregistration checks for the actual uniform-road scenario generator."""

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from formal_scenario import registrations, resolved_geometry_job, ScenarioGeometryError


class FormalScenarioTest(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads((ROOT / "configs/core.json").read_text(encoding="utf-8"))

    def test_full_road_rejects_sparse_candidates_without_changing_the_population(self):
        for density in (1, 2):
            for seed in (100, 101):
                with self.subTest(density=density, seed=seed):
                    with self.assertRaises(ScenarioGeometryError) as failure:
                        registrations(self.cfg, dict(scenario="E0", seed=seed, density_per_km_lane=density))
                    self.assertEqual(failure.exception.report["uniform_spacing_m"], 1000 / density)
                    self.assertEqual(failure.exception.report["eligible_background_pairs"], 0)

    def test_background_and_phase_inputs_are_independent_of_scheme(self):
        for scenario in ("E0", "S0", "S1", "S2"):
            for density in (5, 15, 30):
                job = dict(scenario=scenario, density_per_km_lane=density, seed=100)
                a = registrations(self.cfg, dict(job, scheme="G0"))
                self.assertEqual(a, registrations(self.cfg, dict(job, scheme="G3")))
                vehicles, pairs, events, priors, phases, meta = a
                self.assertEqual(meta["background_prefill_n"], 32 * density)
                background = [p for p in pairs if p["role"] == "background"]
                self.assertEqual(len({p["receiver_id"] for p in background}), 10)
                self.assertEqual(len(phases), len(vehicles))
                self.assertEqual(len(events), self.cfg["scenarios"][scenario]["events"])
                self.assertEqual(len(priors), 1 if scenario == "S0" else 0)
                for member in phases:
                    self.assertGreaterEqual(member["phase_s"], 0.1)
                    self.assertLessEqual(member["phase_s"], 1)

    def test_short_resource_probe_and_long_engineering_geometry(self):
        job = {"scenario": "E0", "seed": 200}
        for count in (16, 64):
            cfg = {**self.cfg, "resource_probe": {"background_vehicles": count, "duration_s": 4}}
            resolved = resolved_geometry_job(cfg, job)
            *_, meta = registrations(cfg, resolved)
            self.assertEqual((meta["uniform_spacing_m"], meta["duration_s"]), (150, 4))
            self.assertIsNone(meta["density_per_km_lane"])
        cfg = {**self.cfg, "engineering_geometry": {"background_vehicles": 16}}
        resolved = resolved_geometry_job(cfg, {**job, "scenario": "S2"})
        *_, meta = registrations(cfg, resolved)
        self.assertEqual(
            (meta["distribution"], meta["target_count"], meta["duration_s"]),
            ("uniform_short_segment", 8, 120),
        )
        self.assertGreaterEqual(meta["eligible_background_pairs"], 10)

    def test_inputs_follow_actual_traffic_seed(self):
        a = registrations(self.cfg, dict(scenario="S1", density_per_km_lane=5, seed=0))
        b = registrations(self.cfg, dict(scenario="S1", density_per_km_lane=5, seed=1))
        self.assertNotEqual(a[0], b[0])
        self.assertNotEqual(a[4], b[4])
        self.assertEqual(a[2], b[2])


if __name__ == "__main__":
    unittest.main()
