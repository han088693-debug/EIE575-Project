from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
import experiment
import watch_e0 as watch


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.config=self.root/"core.json";self.output=self.root/"campaign"
        self.status=self.output/"timeout_monitor/status.json"
        self.attempt=self.root/"old"
        self.record={"request_id":"f01","attempt_id":"old","attempt":str(self.attempt),
                     "probe":"original","state":"engineering_failed","job":[30,20,100]}
        self.state={"version":{"key":"abc"},"requests":[self.record],"status":"engineering_failed"}
        self.index=self.output/"index.json";experiment.save(self.index,self.state)
        self.engine=self.attempt/"logs/engine_status.json"
        experiment.save(self.engine,{"status":"interrupted_timeout","error":"Per-job wall timeout","wall_timeout_s":1800})
        experiment.save(self.attempt/"config_snapshot.json",{"data_origin":"simulation","timing":{"duration_s":120}})
        self.calls=[]
    def evidence(self,last,seconds):
        return {"progress_samples":[(50,60,1100),(61,61,1120)],"projected_total_wall_s":2300,
                "engine_status_sha256":experiment.file_hash(self.engine)}
    def default(self,cfg,output,**opts):
        self.calls.append(opts)
        state=experiment.json_read(self.index)
        if opts.get("retry_failed"):
            self.assertEqual(opts["expected_request"],("f01","engineering_failed","old"))
            self.assertEqual(opts["extend_timeout"],2940)
            state["requests"].append(dict(request_id="f02",state="submitted",job=[30,20,100],timeout_extension={"parent_attempt_id":"old"}))
            state["status"]="submitted"
            experiment.save(self.index,state)
            return {"status":"submitted","current":state["requests"][-1]},40
        return {"status":"selected"},0
    def run_cycle(self,driver=None):
        with patch.object(watch.timeout_retry,"evidence",side_effect=self.evidence):
            return watch.cycle(self.config,self.output,self.status,driver=driver or self.default)
    def test_busy_controller_strictly_passive(self):
        with experiment.locked(self.output):
            r,done,_=self.run_cycle()
        self.assertEqual(r["status"],"observing_active_controller")
        self.assertFalse(done)
        self.assertFalse(self.calls)
    def test_one_timeout_takeover_then_normal_drive(self):
        r,done,_=self.run_cycle()
        self.assertEqual(r["driver_status"],"submitted")
        self.assertFalse(done)
        self.assertTrue(r["owns_continuation"])
        self.assertEqual(r["budget_decisions"]["old"]["new_wall_timeout_s"],2940)
        r,done,code=self.run_cycle()
        self.assertTrue(done);self.assertEqual(code,0);self.assertEqual(r["status"],"selected")
        self.assertNotIn("retry_failed",self.calls[-1])
    def test_second_timeout_other_failure_and_grid_stop(self):
        self.run_cycle()
        revised=experiment.json_read(self.index)
        row=revised["requests"][-1]
        row.update(state="engineering_failed",attempt=str(self.attempt),attempt_id="extension")
        revised["status"]="engineering_failed";experiment.save(self.index,revised)
        r,done,_=self.run_cycle()
        self.assertTrue(done);self.assertIn("already used",r["reason"]);self.assertEqual(len(self.calls),1)
        revised["status"]="grid_exhausted";experiment.save(self.index,revised)
        self.assertEqual(self.run_cycle()[0]["reason"],"grid_exhausted")
        self.record["reason"]="solver failed"
        experiment.save(self.engine,{"status":"failed","error":"bad"})
        experiment.save(self.index,self.state)
        self.assertEqual(self.run_cycle()[0]["reason"],"solver failed")
        self.assertEqual(len(self.calls),1)
    def test_midair_failure_never_submits_before_recorded_timeout(self):
        state=self.state;old=state["requests"][-1]
        old["state"]="running";state["status"]="running";experiment.save(self.index,state)
        def read_only(config,output,**kw):
            self.assertNotIn("retry_failed",kw)
            self.assertEqual(kw["expected_request"][1],"running")
            old["state"]="engineering_failed";state["status"]="engineering_failed";experiment.save(self.index,state)
            return {"status":"engineering_failed","current":old},20
        r,done,_=self.run_cycle(read_only)
        self.assertFalse(done)
        self.assertFalse(r.get("owns_continuation"))
        self.assertEqual(self.run_cycle()[0]["driver_status"],"submitted")
    def test_synthetic_provenance_and_stale_head(self):
        experiment.save(self.attempt/"config_snapshot.json",{"data_origin":"synthetic","timing":{"duration_s":120}})
        r,done,_=self.run_cycle()
        self.assertTrue(done);self.assertIn("real simulation",r["reason"]);self.assertFalse(self.calls)
        data,code=watch.advance_if_current(self.config,self.output,expected_request=("different",None,None),expected_campaign="abc")
        self.assertEqual((data["status"],code),("controller_advanced",40))
    def test_durable_stop_does_not_relaunch(self):
        self.state["status"]="invalid_measurement";self.record["state"]="invalid_measurement"
        self.record["reason"]="wrong payload";experiment.save(self.index,self.state)
        first=self.run_cycle();second=self.run_cycle()
        self.assertEqual(first[0],second[0]);self.assertFalse(self.calls)

    def test_bound_readiness_only_resumes_matching_failed_watcher_once(self):
        log=self.root/"readiness.out"
        binding={"pid":123,"create_time":1,"options":{"--config":str(self.config),"--e0-dir":str(self.output)},"log":str(log)}
        monitor={"budget_decisions":{"old":{"parent_request":"f01"}}}
        folder=self.output/"timeout_monitor";folder.mkdir()
        def drive(config,e0,dst,**kwargs):
            self.calls.append((config,e0,dst));return {"status":"awaiting_durable_recovery"},40
        self.assertEqual(watch.handoff(folder,binding,monitor,live=lambda:True,drive=drive)[0]["status"],"original_readiness_active")
        log.write_text('{"status":"e0_blocked","reason":"engineering_failed","request_id":"f01"}\n')
        one=watch.handoff(folder,binding,monitor,live=lambda:False,drive=drive)
        two=watch.handoff(folder,binding,monitor,live=lambda:False,drive=drive)
        self.assertEqual(one,two);self.assertEqual(self.calls,[(self.config,self.output,None)])
        self.assertEqual(one[0]["result"]["status"],"awaiting_durable_recovery")
        other=self.root/"other";other.mkdir()
        log.write_text('{"status":"e0_blocked","reason":"engineering_failed","request_id":"different"}\n')
        self.assertEqual(watch.handoff(other,binding,monitor,live=lambda:False,drive=drive)[0]["status"],"handoff_stopped")
        self.assertEqual(len(self.calls),1)


if __name__=="__main__":unittest.main()
