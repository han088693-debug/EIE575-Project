import copy
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "tools"))
from experiment_config import load_config, validate_config


class CandidateConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_config(PROJECT / "configs" / "core.json")

    def test_candidate_and_resolution(self):
        self.assertEqual(self.cfg["stage"], "candidate")
        for group, key, value in (("timing","deadline_s",2.1),("control","beta",3),("timing","duration_s",100),("timing","motion_step_s",0.05),("seeds","acceptance",0)):
            with self.subTest(parameter=key):
                cfg = copy.deepcopy(self.cfg)
                cfg[group][key] = value
                with self.assertRaises(ValueError):
                    validate_config(cfg)

    def test_control_interval_does_not_change_max_submission_rate(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["timing"]["control_step_s"] = 0.05
        validate_config(cfg)
        self.assertEqual(1 / cfg["control"]["r_max_hz"], 0.1)

    def test_unselected_loads_cannot_be_frozen(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["stage"] = "frozen"
        with self.assertRaises(ValueError):
            validate_config(cfg)
