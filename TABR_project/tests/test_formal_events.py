"""Synthetic gold cases for the preregistered formal event analysis."""
import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from formal_events import analyze_events


HEADERS = {
    "inputs/events": "planned_event_id sender_id planned_time_s target_speed_mps brake_duration_s deadline_s observation_s",
    "inputs/pairs": "pair_id sender_id receiver_id role planned_event_id",
    "logs/event_log": "planned_event_id sender_id t0_lower_bound t0 event_status exclusion_reason",
    "logs/scenario_truth": "t vehicle_id speed_mps",
    "logs/detection_log": "sender_id event_id t_detect event_valid_until",
    "logs/rx_log": "receiver_id sender_id seq t_rx t_gen state_valid state_accepted event_valid event_id event_detect_time event_valid_until",
}


class FormalEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.attempt = Path(self.tmp.name)
        self.cfg = json.loads((ROOT / "configs/core.json").read_text(encoding="utf-8-sig"))
        self.cfg["timing"].update(warmup_s=2, duration_s=14, time_resolution_s=1e-9)
        self.tables = {name: [] for name in HEADERS}

    def setup_scenario(self, scenario):
        self.cfg["scenario"] = scenario
        n = self.cfg["scenarios"][scenario]["events"]
        for i in range(n):
            sender, receiver, name = f"s{i}", f"r{i}", f"e{i}"
            self.tables["inputs/events"].append([name, sender, 2, 0, 3.704, 0.8, 2])
            self.tables["inputs/pairs"].append([f"p{i}", sender, receiver, "target", name])
            self.tables["logs/event_log"].append([name, sender, 2, 2.1, "occurred", ""])
            self.tables["logs/detection_log"].append([sender, 0, 2.1, 4.1])
            for k in range(140):
                t = k/10
                v = max(0, 80/3.6 - 6 * max(0, t-2))
                self.tables["logs/scenario_truth"].extend([[t, sender, v], [t, receiver, 20]])

    def rx(self, r, g, seq=0, receiver="r0", sender="s0", accepted=1, event=1,
           valid=1, ident=0, detect=2.1, until=4.1):
        self.tables["logs/rx_log"].append([receiver, sender, seq, r, g, valid, accepted, event, ident, detect, until])

    def write(self):
        for name, header in HEADERS.items():
            path = self.attempt / (name + ".csv")
            path.parent.mkdir(exist_ok=True)
            with path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(header.split())
                writer.writerows(self.tables[name])

    def result(self):
        self.write()
        return analyze_events(self.attempt, self.cfg)

    def test_s0_and_e0_have_no_event_denominator(self):
        for scenario in ("S0", "E0"):
            self.setup_scenario(scenario)
            res = self.result()
            self.assertEqual(res["events"], [])
            self.assertEqual(res["run"]["event_pair_n"], 0)
            for key in ("deadline_miss_fraction", "conditional_mean_D_info_s", "restricted_mean_wait_s"):
                self.assertIsNone(res["run"][key])

    def test_exact_and_after_deadline(self):
        self.setup_scenario("S1")
        self.rx(2.9, 2.85)
        trace = self.result()["events"][0]
        self.assertEqual(trace["D_info_s"], 0.8)
        self.assertFalse(trace["deadline_miss"])
        self.assertTrue(trace["at_lower_endpoint"]["deadline_miss"])
        self.assertEqual(trace["fixed_info_delay_interval_s_closed_open"], [0.8,0.9])
        self.assertTrue(trace["classification_or_selection_sensitive"])
        self.assertEqual(trace["physical_classification"], "unresolved")
        self.tables["logs/rx_log"][0][3] = "2.900000001"
        self.assertTrue(self.result()["events"][0]["deadline_miss"])

    def test_window_and_expiry_end_are_inclusive(self):
        self.setup_scenario("S1")
        self.rx(4.1,4)
        trace = self.result()["events"][0]
        self.assertEqual(trace["D_info_s"],2)
        self.assertTrue(trace["at_lower_endpoint"]["no_receive"])
        self.tables["logs/rx_log"][0][3] = "4.100000001"
        trace = self.result()["events"][0]
        self.assertIsNone(trace["D_info_s"])
        self.assertTrue(trace["right_censored"])
        self.assertEqual(trace["restricted_wait_s"], 2)
        self.cfg["timing"]["event_validity_s"] = 0.8
        self.tables["logs/detection_log"][0][-1] = 2.9
        self.tables["logs/rx_log"].clear()
        self.rx(2.9,2.8,until=2.9)
        self.assertTrue(self.result()["events"][0]["delivered"])
        self.tables["logs/rx_log"][0][3] = "2.900000001"
        trace = self.result()["events"][0]
        self.assertTrue(trace["no_receive"])
        self.assertIsNotNone(trace["D_update_s"])

    def test_missing_or_late_detection_is_a_valid_miss(self):
        self.setup_scenario("S1")
        self.tables["logs/detection_log"].clear()
        self.rx(2.21,2.2,event=0,ident=-1,detect=-1,until=-1)
        result = self.result()
        trace, group = result["events"][0], result["run"]
        self.assertEqual((trace["physical_occurred"], trace["detected"], trace["delivered"]), (True,False,False))
        self.assertEqual(trace["D_update_s"],0.11)
        self.assertEqual(group["not_detected_fraction"],1)
        self.assertEqual(group["restricted_mean_wait_s"],2)
        self.assertIsNone(group["conditional_mean_D_info_s"])
        self.tables["logs/detection_log"].append(["s0",0,4.2,6.2])
        trace = self.result()["events"][0]
        self.assertTrue(trace["detected"])
        self.assertTrue(trace["deadline_miss"])
        self.assertTrue(trace["no_receive"])

    def test_lower_endpoint_reselects_post_onset_accepted_state(self):
        self.setup_scenario("S1")
        self.rx(2.19,2.05,event=0)
        self.rx(2.20,2.19,seq=1,accepted=0,event=0)
        self.rx(2.23,2.22,seq=2)
        t = self.result()["events"][0]
        self.assertEqual((t["first_update_seq"],t["at_lower_endpoint"]["first_update_seq"]),(2,0))
        self.assertEqual(t["D_update_s"],0.13)
        self.assertEqual(t["at_lower_endpoint"]["D_update_s"],0.19)

    def test_s2_same_local_id_cross_lane_fanout_and_denominator(self):
        self.setup_scenario("S2")
        self.rx(2.25,2.2,receiver="r1")   # s0 -> r1 is not a registered primary pair
        self.rx(2.3,2.2,sender="s1",receiver="r1")
        self.rx(3.0,2.95,sender="s3",receiver="r3")
        self.tables["logs/event_log"].reverse()
        self.tables["logs/detection_log"].reverse()
        report = self.result()
        run = report["run"]
        self.assertEqual((run["physical_event_n"],run["event_pair_n"],run["delivered_n"]), (4,4,2))
        self.assertEqual(run["deadline_miss_fraction"], 0.75)
        self.assertEqual(run["no_receive_fraction"], 0.5)
        self.assertAlmostEqual(run["restricted_mean_wait_s"], (2+0.2+2+0.9)/4)
        self.assertEqual((report["earliest_t0_s"],report["last_stop_s"]), (2.1,5.7))
        self.cfg["scenarios"]["S2"]["target_pairs"] = 5
        self.tables["inputs/pairs"].append(["p4","s0","r1","target","e0"])
        extra = self.result()
        self.assertEqual((extra["run"]["physical_event_n"],extra["run"]["event_pair_n"]), (4,5))
        self.assertEqual(extra["events"][1]["D_info_s"], 0.15)

    def test_duplicate_receptions_do_not_create_new_state_or_event(self):
        self.setup_scenario("S1")
        self.rx(2.2,2.15,event=0)
        self.rx(2.25,2.15,accepted=0)
        t = self.result()["events"][0]
        self.assertTrue(t["no_receive"])
        self.assertEqual(t["D_update_s"],0.1)
        self.tables["logs/rx_log"][1][6] = 1
        with self.assertRaisesRegex(ValueError,"stale state"):
            self.result()

    def test_ambiguous_or_malformed_keys_invalid(self):
        for name in ("inputs/events","inputs/pairs","logs/event_log","logs/detection_log"):
            with self.subTest(name=name):
                self.tables = {name: [] for name in HEADERS}
                self.setup_scenario("S1")
                self.tables[name].append(self.tables[name][0])
                with self.assertRaises(ValueError):
                    self.result()

    def test_motion_strength_presence_and_hold_are_required(self):
        for mutation, message in (("weak","strength"),("never","speed drop"),("missing","absent"),
                                  ("gap","motion tick"),("restart","not maintained"),("t0","speed drop"),
                                  ("pre_speed","cruise"),("invalid","did not occur")):
            with self.subTest(mutation=mutation):
                self.tables = {name: [] for name in HEADERS}
                self.setup_scenario("S1")
                truth = self.tables["logs/scenario_truth"]
                if mutation == "missing":
                    truth[:] = [r for r in truth if r[1] != "r0"]
                if mutation == "gap":
                    truth[:] = [r for r in truth if r[:2] != [2.4,"s0"]]
                if mutation == "t0":
                    self.tables["logs/event_log"][0][3] = 2.2
                if mutation == "invalid":
                    self.tables["logs/event_log"][0][4] = "not_occurred"
                for row in truth:
                    t, sender, _ = row
                    if sender != "s0":
                        continue
                    if mutation == "weak" and 2<t<5.7:
                        row[2] = 80/3.6-2*(t-2)
                    if mutation == "never":
                        row[2] = 80/3.6
                    if mutation == "pre_speed" and t == 1.1:
                        row[2] = 18
                    if mutation == "restart" and t>10:
                        row[2] = 2
                with self.assertRaisesRegex(ValueError,message):
                    self.result()

    def test_missing_files_short_window_or_run_invalid(self):
        self.setup_scenario("S1")
        self.write()
        (self.attempt/"logs/rx_log.csv").unlink()
        with self.assertRaisesRegex(ValueError,"Cannot read"):
            analyze_events(self.attempt,self.cfg)
        self.tables["inputs/events"][0][6]=0.7
        with self.assertRaisesRegex(ValueError,"observation"):
            self.result()
        self.tables["inputs/events"][0][6]=2
        self.cfg["timing"]["duration_s"]=11
        with self.assertRaisesRegex(ValueError,"too short"):
            self.result()


if __name__ == "__main__":
    unittest.main()
