"""Synthetic log fixture for the observed masked run-cap and false-positive boundaries."""
import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
from timeout_recovery import buffered_timeout_progress, classify


class MaskedCapTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.a=Path(self.tmp.name); self.logs=self.a/"logs";self.logs.mkdir();(self.a/"inputs").mkdir()
        cm=self.a/"inputs/.cmdenv-log";cm.touch()
        msg=f"[WinError 32] {str(cm)!r} -> {str(self.logs/'cmdenv-internal.log')!r}"
        self.rec={"state":"engineering_failed","attempt":str(self.a),"attempt_id":"test","reason":msg}
        self.eng={"status":"running","engine_complete":False,"return_code":None,"wall_timeout_s":1800}
        now=datetime.now(timezone.utc)
        self.manifest={"attempt_id":"test","scope":"engineering","data_origin":"simulation","scenario":"E0","status":"engine_error",
                       "error":msg,"started_at":now.isoformat(),"completed_at":(now+timedelta(seconds=1801)).isoformat(),"run_id":"test"}
        self.setup_manifest()
        (self.a/"config_snapshot.json").write_text(json.dumps({"data_origin":"simulation","timing":{"duration_s":120}}))
        for file,field in (("control_log.csv","tick_time"),("state_log.csv","t"),("busy_audit.csv","transition_time")):
            (self.logs/file).write_text("run_id,"+field+"\ntest,58\ntest,59.2\ntest,")
        self.resource(1800.1)
    def setup_manifest(self):
        (self.a/"run_manifest.json").write_text(json.dumps(self.manifest))
    def resource(self,end):
        with (self.logs/"resource_trace.csv").open("w",newline="") as f:
            w=csv.writer(f);w.writerow("t_wall_s t_sim_s cpu_seconds process_count private_bytes handles".split())
            for row in ((1,.1,1),(800,25,790),(1200,40,1180),(1700,56,1690),(end,56,end-2)):
                w.writerow((*row,11,100000,1100))
    def test_derived_classifier_keeps_original_and_rejects_mismatched_error_and_run(self):
        snapshot=(self.a/"run_manifest.json").read_bytes()
        d=classify(self.rec,self.eng)
        self.assertEqual(d["status"],"interrupted_timeout");self.assertEqual(self.eng["status"],"running")
        self.assertEqual(snapshot,(self.a/"run_manifest.json").read_bytes())
        self.assertEqual(d["derived_timeout"]["wall_paired_physics"][-1],(59.2,1800.1))
        self.assertEqual(classify(self.rec|{"reason":"unrelated"},self.eng),self.eng)
        self.assertEqual(classify(self.rec|{"attempt_id":"other"},self.eng),self.eng)
    def test_no_cap_or_no_independent_advancing_physics_is_not_recovered(self):
        self.resource(1750)
        self.assertEqual(classify(self.rec,self.eng),self.eng)
        self.resource(1800.1)
        (self.logs/"control_log.csv").write_text("run_id,tick_time\ntest,55.0\ntest,59")
        self.assertEqual(classify(self.rec,self.eng),self.eng)
    def test_shorter_or_successful_run_is_not_recovered(self):
        self.manifest["completed_at"]=self.manifest["started_at"];self.setup_manifest()
        self.assertEqual(classify(self.rec,self.eng),self.eng)
        self.assertEqual(classify(self.rec,self.eng|{"return_code":0})["status"],"running")

    def test_confirmed_wall_cap_can_use_independent_ticks_if_console_is_buffered(self):
        reason = "Per-job wall timeout"
        self.manifest["error"] = reason
        self.setup_manifest()
        record = dict(self.rec, reason=reason)
        engine = dict(self.eng, status="interrupted_timeout", error=reason)
        proof = buffered_timeout_progress(record, engine)["derived_timeout"]
        self.assertEqual(proof["reason"], "buffered_cmdenv_at_confirmed_wall_cap")
        self.assertEqual(proof["wall_paired_physics"][-1], (59.2, 1800.1))
        self.assertEqual(buffered_timeout_progress(dict(record, reason="other"), engine), engine)
        self.resource(1750)
        self.assertEqual(buffered_timeout_progress(record, engine), engine)


if __name__=="__main__":unittest.main()
