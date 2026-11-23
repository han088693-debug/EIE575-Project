"""Cross-file transport, provenance, schema and integration regression checks."""

import csv
import json
from pathlib import Path
import tempfile
import unittest

from formal_fixtures import fixture_config, write_fixture
from formal_analyze import analyze_attempt


class FormalAttemptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def make(self, scenario="S1", case="on_time"):
        return write_fixture(Path(self.temp.name) / case, fixture_config(scenario), case)

    def edit(self, root, table, fn):
        path = root / "logs" / (table + ".csv")
        with path.open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            fields = reader.fieldnames
            rows = list(reader)
        fn(rows)
        with path.open("w", encoding="utf-8", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def test_event_outcomes_remain_valid(self):
        for case, miss, none, det in [
            ("on_time", 0, 0, 0),
            ("late", 1, 0, 0),
            ("no_receive", 1, 1, 0),
            ("not_detected", 1, 1, 1),
        ]:
            with self.subTest(case=case):
                result = analyze_attempt(self.make(case=case))
                self.assertEqual(result["data_validity"], "valid")
                self.assertEqual(result["data_origin"], "synthetic")
                self.assertEqual(
                    [result["run"][k] for k in ("deadline_miss_n", "no_receive_n", "not_detected_n")],
                    [miss, none, det],
                )
                self.assertEqual(result["run"]["event_pair_n"], 1)

    def test_prior_boundary_and_counts(self):
        for case, rx in [("prior_only", 0), ("prior_replay", 4), ("prior_first_seq0", 1)]:
            with self.subTest(case=case):
                result = analyze_attempt(self.make("S0", case))
                self.assertEqual(result["transport"]["all_rx"], rx)
                self.assertEqual(result["transport"]["given_priors"], 1)
                self.assertIsNone(result["run"]["deadline_miss_fraction"])
                self.assertEqual(result["run"]["event_pair_n"], 0)

    def test_registered_multi_sender_and_short_residence(self):
        result = analyze_attempt(self.make("S2"))
        self.assertEqual(result["run"]["event_pair_n"], 4)
        self.assertEqual(result["run"]["delivered_n"], 4)
        result = analyze_attempt(self.make(case="short_lived"))
        self.assertEqual(result["transport"]["vehicles"], 5)

    def test_invalid_fixtures(self):
        for case in ("missing_log", "wrong_bits", "truncated"):
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    analyze_attempt(self.make(case=case))

    def test_payload_mutation_is_invalid(self):
        root = self.make()
        self.edit(root, "rx_log", lambda rs: rs[0].update(position_m="111"))
        with self.assertRaisesRegex(ValueError, "unmatched TX"):
            analyze_attempt(root)

    def test_one_missing_callback_from_trimmed_log(self):
        root = self.make()
        self.edit(root, "rx_log", lambda rs: rs.clear())
        with self.assertRaises(ValueError):
            analyze_attempt(root)

    def test_all_rx_counter_is_independent(self):
        root = self.make(case="no_receive")
        self.edit(root, "lifecycle", lambda rs: rs[0].update(rx_count="19"))
        with self.assertRaisesRegex(ValueError, "counts do not reconcile"):
            analyze_attempt(root)

    def test_same_packet_key_is_invalid(self):
        root = self.make()
        self.edit(root, "tx_log", lambda rs: rs.append(dict(rs[0])))
        with self.assertRaisesRegex(ValueError, "Duplicate TX"):
            analyze_attempt(root)

    def test_duplicate_record_cannot_replace_s0_prior(self):
        root = self.make("S0", "prior_replay")
        self.edit(root, "rx_log", lambda rs: rs[-1].update(state_accepted="1", state_valid="1"))
        with self.assertRaisesRegex(ValueError, "State acceptance"):
            analyze_attempt(root)

    def test_state_source_requires_logged_rx(self):
        root = self.make("S0", "prior_only")

        def wrong(rows):
            r = next(r for r in rows if r["state_source"] == "given_prior")
            r.update(state_source="received", last_seq="0")

        self.edit(root, "state_log", wrong)
        with self.assertRaisesRegex(ValueError, "source/sequence"):
            analyze_attempt(root)

    def test_mixed_run_in_last_row(self):
        root = self.make()
        self.edit(root, "control_log", lambda rs: rs[-1].update(run_id="another"))
        with self.assertRaisesRegex(ValueError, "run_id mismatch"):
            analyze_attempt(root)

    def test_synthetic_cannot_be_formal(self):
        root = self.make()
        path = root / "run_manifest.json"
        m = json.loads(path.read_text(encoding="utf-8"))
        m["scope"] = "formal"
        path.write_text(json.dumps(m), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Synthetic input"):
            analyze_attempt(root)

    def test_e0_requires_real_supply_observations(self):
        root = self.make("E0")
        self.assertEqual(analyze_attempt(root)["run"]["event_pair_n"], 0)
        self.edit(root, "local_supply", lambda rs: rs.clear())
        with self.assertRaisesRegex(ValueError, "local supply"):
            analyze_attempt(root)


if __name__ == "__main__":
    unittest.main()
