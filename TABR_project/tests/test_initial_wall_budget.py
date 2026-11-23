import json
from hashlib import sha256
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"tools"))
from e0_budget_benchmarks import pinned_e0_benchmarks
from initial_wall_budget import select_initial_budget


class FirstBudgetTest(unittest.TestCase):
    base = dict(density=25,duration_s=120,slots=2,authorized_cap_s=5400,machine_id="host",cost_group="E0")

    def row(self, **changes):
        return dict(dict(attempt_id="d30",machine_id="host",cost_group="E0",accepted=True,density=30,
                         duration_s=120,slots=1,wall_seconds=3873.3221012),**changes)

    def plan(self, rows, **changes):
        return select_initial_budget(rows,**{**self.base,**changes})

    def test_real_heavier_reference_and_full_authorized_two_slot_ceiling(self):
        rows = [self.row(),self.row(attempt_id="d30-seed101",wall_seconds=3610.2130059)]
        old = self.plan(rows,slots=1,fallback_s=1800)
        self.assertEqual((old["wall_timeout_s"],old["basis"]),(4920,"nearest_heavier_density"))
        dual = self.plan(rows,fallback_s=1800)
        self.assertEqual((dual["wall_timeout_s"],dual["budget_before_cap_s"]),(5400,9780))
        self.assertTrue(dual["concurrency_estimated"])
        self.assertTrue(dual["benchmark_exceeds_cap"])

    def test_same_density_and_measured_concurrency(self):
        rows=[self.row(),self.row(attempt_id="d25a",density=25,slots=1,wall_seconds=2200),
              self.row(attempt_id="d25b",density=25,slots=2,wall_seconds=2500),
              self.row(attempt_id="d25c",density=25,slots=2,wall_seconds=2700)]
        plan=self.plan(rows)
        self.assertEqual((plan["basis"],plan["reference_slots"],plan["wall_timeout_s"]),("same_density",2,3480))
        self.assertEqual(plan["benchmark_attempt_ids"],["d25b","d25c"])
        self.assertFalse(plan["concurrency_estimated"])

    def test_missing_comparable_measurements_spends_allowed_cap(self):
        rows=[self.row(accepted=False),self.row(machine_id="other"),self.row(cost_group="S2"),
              self.row(duration_s=20),self.row(density=20)]
        plan=self.plan(rows)
        self.assertEqual((plan["wall_timeout_s"],plan["benchmark_attempt_ids"]),(5400,[]))
        self.assertIsNone(plan["estimated_wall_s"])
        self.assertEqual(self.plan([self.row()],density=50)["basis"],"unmeasured_size_or_horizon")

    def test_cap_and_stratum_floor_are_respected_and_duplicates_detected(self):
        self.assertEqual(self.plan([self.row()],authorized_cap_s=1800)["wall_timeout_s"],1800)
        self.assertEqual(self.plan([self.row()],slots=1,fallback_s=5200)["wall_timeout_s"],5200)
        with self.assertRaises(ValueError):
            self.plan([self.row(),self.row(wall_seconds=1)])
        with self.assertRaises(ValueError):
            self.plan([],authorized_cap_s=None)
        with self.assertRaises(ValueError):
            self.plan([],slots=True)
        with self.assertRaises(ValueError):
            self.plan([self.row(wall_seconds=float("nan"))])

    def test_bad_pin_machine_and_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            pinned=root/"evidence.json"
            data=dict(format="e0-closed-serial-wall-v1",machine_id="host",cost_group="E0/G0/registered_pairs",
                      measured_slots=1,closed_dir="closed",closed_snapshot_sha256="bad")
            pinned.write_text(json.dumps(data))
            pin=sha256(pinned.read_bytes()).hexdigest()
            (root/"closed").mkdir()
            (root/"closed/snapshot.json").write_text("{}")
            for supplied,host,reason in [(pin,"other","origin"),("wrong","host","SHA-256"),(pin,"host","snapshot")]:
                with self.assertRaisesRegex(ValueError,reason):
                    pinned_e0_benchmarks(pinned,supplied,project=root,machine_id=host)


if __name__ == "__main__":
    unittest.main()
