import copy, json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
SOURCE=Path(__file__).resolve().parents[1]/"tools"
sys.path.insert(0,str(SOURCE))
import calibration_driver as driver
import timeout_retry as retry


def save(p,x):
    p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x))


class TimeoutExtensionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.proj=self.root/"project";self.out=self.root/"campaign"
        self.base="p3e0_abc_d30_w20_s100"; self.key=(30,20,100)
        self.parent=self.root/"old"; self.logs=self.parent/"logs"
        self.logs.mkdir(parents=True)
        self.manifest={"attempt_id":"old","scope":"engineering","scenario":"E0","simulation_job_key":"k"}
        save(self.parent/"run_manifest.json",self.manifest)
        self.cfg={"control":{"beta":2},"timing":{"duration_s":120},"execution":{"wall_timeout_s":1800}}
        save(self.parent/"config_snapshot.json",self.cfg)
        save(self.proj/"configs/p3/abc/warm20.json",self.cfg)
        save(self.proj/"configs/formal_probes.json",{"probes":{self.base:{"config":"p3/abc/warm20.json","seed":100}}})
        self.record={"attempt_id":"old","scope":"engineering","job":list(self.key),"probe":self.base,"request_id":"a01",
                     "state":"engineering_failed","attempt":str(self.parent)}
        self.state={"requests":[self.record],"status":"engineering_failed"}
        self.engine={"status":"interrupted_timeout","error":"Per-job wall timeout","wall_timeout_s":1800,"port":123,
                     "pids":[{"pid":2000000000,"create_time":1}],"peak_private_bytes":123456,"peak_handles":100}
        save(self.logs/"engine_status.json",self.engine)
        (self.logs/"simulation.log").write_text("\n".join(f"** Event #{x*1000}   t={x/25:.2f}   Elapsed: {x}s" for x in (1785,1790,1795)))
        (self.logs/"resource_trace.csv").write_text("t_wall_s,private_bytes,handles,process_count\n1799.5,1000,12,3\n1799.9,1000,12,3\n")
        self.calls=[]
    def select(self,n):
        return retry.select_probe(self.proj,self.out,self.state,self.base,self.key,n,driver.immutable,driver.preserve)
    def test_only_once_idempotent_and_scientific_equivalence(self):
        with self.assertRaisesRegex(ValueError,"Specify"): self.select(None)
        name,r=self.select(4000)
        self.assertEqual(name,"x"+driver.experiment.identity([self.base,"timeout-extension-1"])[:14])
        self.assertEqual(self.select(None),(name,r))
        self.assertEqual(len(self.state["timeout_extensions"]),1)
        with self.assertRaisesRegex(ValueError,"differs"): self.select(4001)
        received=json.loads((self.out/r["receipt"]).read_text())
        self.assertEqual(received["parent_simulation_job_key"],"k")
        self.assertAlmostEqual(received["projected_total_wall_s"],3000)
        self.assertEqual(received["old_wall_timeout_s"],1800)
        spec=json.loads((self.proj/"configs/formal_probes.json").read_text())["probes"][name]
        extended=json.loads((self.proj/"configs"/spec["config"]).read_text())
        self.assertEqual(extended,self.cfg|{"execution":{"wall_timeout_s":4000}})
        self.assertEqual(json.loads((self.parent/"config_snapshot.json").read_text()),self.cfg)
        self.state["requests"].append(dict(self.record,probe=name))
        with self.assertRaisesRegex(ValueError,"already submitted"): self.select(5000)
    def test_not_growing_or_wrong_timeout(self):
        (self.logs/"simulation.log").write_text("** Event #1   t=2.0  Elapsed: 900s\n** Event #1   t=2.0  Elapsed: 1798s")
        with self.assertRaisesRegex(ValueError,"advancing"): self.select(4000)
        self.engine["error"]="Veins launcher listener timed out"
        save(self.logs/"engine_status.json",self.engine)
        with self.assertRaisesRegex(ValueError,"not interrupted"): self.select(4000)
        self.assertEqual(self.select(None),(self.base,None))
    def test_weak_owner_evidence_and_disk(self):
        with patch("timeout_retry.shutil.disk_usage",return_value=type("D",(),{"free":0})()):
            with self.assertRaisesRegex(ValueError,"Insufficient disk"): self.select(4000)
        self.engine["pids"]=[]
        save(self.logs/"engine_status.json",self.engine)
        with self.assertRaisesRegex(ValueError,"owned PID"):self.select(4000)
    def test_actual_driver_advance_resumes_same_request_after_disconnect(self):
        def rpc(cfg,name,args):
            self.calls.append((name,args))
            if len(self.calls)==1:raise ConnectionError("lost after queue")
            spec="x"+driver.experiment.identity([self.base,"timeout-extension-1"])[:14]
            if name=="get_status":return dict(request_id=args["request_id"],probe=spec,state="running")
            return dict(request_id=args["request_id"])
        decision={"status":"pending","next_runs":[dict(density_per_lane_km=30,warmup_s=20,seed=100)],"completed_unique_runs":0}
        with patch.object(driver,"load_reports",return_value=[]), patch.object(driver.calibrate,"decide",return_value=decision):
            args=(self.proj,self.cfg,self.out,self.state,{"key":"test"}, {self.key:self.base},rpc)
            with self.assertRaises(ConnectionError):driver.advance(*args,retry_failed=True,extend_timeout=4000)
            self.assertEqual(self.state["requests"][-1]["state"],"submitting")
            data,rc=driver.advance(*args)
            self.assertEqual((data["status"],rc),("running",40))
            self.assertEqual(self.calls[0],self.calls[1])
            self.assertEqual(len(self.state["requests"]),2)


if __name__=="__main__":unittest.main()
