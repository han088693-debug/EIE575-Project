import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from formal_state import Clock, aggregate_controls, analyze_state, audit_controller, group_series, motion_mask, recovery, rows, tx_metrics


def write(path, fields, data):
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(fields.split())
        w.writerows(data)


class FormalStateTests(unittest.TestCase):
    @staticmethod
    def build(attempt, scenario="E0"):
        cfg = json.loads((ROOT / "configs/core.json").read_text(encoding="utf-8-sig"))
        cfg.update(scenario=scenario, scheme="G0", data_origin="synthetic")
        cfg["timing"].update(duration_s=1.0,warmup_s=.3,stationary_eval_s=.35,post_event_s=.35,observation_s=.2)
        cfg["control"].update(lookback_s=.2)
        inputs, logs = attempt / "inputs", attempt / "logs"
        inputs.mkdir()
        logs.mkdir()
        write(inputs / "pairs.csv","pair_id sender_id receiver_id role eval_start eval_end",
              [("p","lead","follow","target",0,1),("q","b1","b2","background",0,1)])
        base = ["lead","follow","b1","b2"]
        life = [(v,0,1,0,0,int(v in ("lead","follow"))) for v in base]
        life.append(("brief",.295,.31,0,0,0))
        life.append(("asyn",.025,.875,0,0,0))
        write(logs/"lifecycle.csv","vehicle_id enter_s exit_s tx_count rx_count busy_audited",life)
        write(logs/"scenario_truth.csv","t vehicle_id position_m speed_mps",
              [(f"{i/10:.1f}",v,0,0) for i in range(11) for v in base])
        write(logs/"busy_audit.csv","vehicle_id transition_time busy_state",
              [("lead",0,0),("follow",0,0),("lead",.24,1),("lead",.36,0)])
        ctrl, state = [], []
        previous = {}
        for i in range(1,10):
            t = i/10
            for v in base:
                raw = (0.6 if i in (3,4) and v=="lead" else 0)
                bar = .8*previous[v]+.2*raw if i!=1 else raw
                previous[v] = bar
                ctrl.append((v,t,(i-1)/10,t,raw*.1,raw,bar,10,1))
            if i<=8:
                ctrl.append(("asyn",f"{t+.025:.3f}",f"{(i-1)/10+.025:.3f}",f"{t+.025:.3f}",0,0,0,10,1))
        for i in range(10):
            t = i/10
            state.append((t,"lead","follow",1,"given_prior" if scenario=="S0" else "received",0,3,0,3,t))
            state.append((t,"b1","b2",0,"unknown","","","","",""))
        write(logs/"control_log.csv","vehicle_id tick_time window_start window_end busy_duration_s C Cbar r W",ctrl)
        write(logs/"state_log.csv","t sender_id receiver_id known state_source last_state_gen_time p_last_m v_last_mps p_hat_m Age_s",state)
        write(logs/"tx_log.csv","sender_id seq t_submit",[])
        return cfg

    def test_negative_error_zero_known_asynchronous_and_partial_exposure(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = self.build(root)
            result = analyze_state(root,cfg,{})
            first, second = result["pairs"]["p"], result["pairs"]["q"]
            self.assertAlmostEqual(first["exposure_s"],.35)
            self.assertEqual(first["mean_abs_error_m"],3)
            self.assertEqual(first["error_above_fraction_of_known"]["2"],1)
            self.assertAlmostEqual(first["mean_age_s"],(.3+.4+.5+.6*.5)/3.5)
            self.assertEqual(second["unknown_fraction"],1)
            self.assertIsNone(second["mean_abs_error_m"])
            self.assertIsNone(second["error_above_fraction_of_known"]["2"])
            self.assertAlmostEqual(result["vehicles"]["lead"]["mean_cbr"],.06/.35)
            self.assertEqual(result["vehicles"]["lead"]["p95_cbr"],.6)
            self.assertAlmostEqual(result["vehicles"]["asyn"]["cbr_exposure_s"],.35)
            self.assertIsNone(result["vehicles"]["brief"]["mean_cbr"])
            self.assertEqual(result["busy_audit"]["audited_windows"],18)
            self.assertIsNone(result["s2_recovery"])

    def test_missing_observations_fail(self):
        for file, marker, error in [
                ("state_log.csv","0.4,lead,follow,","incomplete state"),
                ("scenario_truth.csv","0.4,lead,","missing independent truth"),
                ("control_log.csv","lead,0.4,","invalid control window"),
                ("busy_audit.csv","lead,0.36,0","busy integral mismatch")]:
            with self.subTest(file=file), tempfile.TemporaryDirectory() as d:
                root=Path(d)
                cfg=self.build(root)
                path=root/"logs"/file
                original=path.read_text().splitlines()
                revised=[line for line in original if not line.startswith(marker)]
                self.assertLess(len(revised),len(original))
                path.write_text("\n".join(revised)+"\n")
                with self.assertRaisesRegex(ValueError,error):
                    analyze_state(root,cfg,{})

    def test_h2_historical_independent_g3_truth(self):
        folder=ROOT/"verification/g1_speed_revision_20261021/G3"
        clock=Clock({"time_resolution_s":1e-12})
        tick=clock.tick
        points={tick(r["t"]):(float(r["position_m"]),float(r["speed_mps"])) for r in
                rows(folder/"scenario_truth.csv","t vehicle_id position_m speed_mps") if r["vehicle_id"]=="lead"}
        m=motion_mask(points,clock,tick(.1),tick(.5),.5,4,1e-6)
        t0=tick(next(rows(folder/"event_log.csv","t0"))["t0"])
        self.assertFalse(any(active for t,(active,_,_) in m.items() if t0<=t<=t0+tick(2)))
        times=[t for t,(active,_,_) in m.items() if t0<=t<t0+tick(10) and active]
        self.assertEqual(times,[tick(v) for v in (23.9,24.0,24.1,24.2,24.3)])
        near_zero=[r for r in rows(folder/"control_log_lead.csv","B S") if r["B"]=="0" and 0<float(r["S"])<1e-8]
        self.assertEqual(len(near_zero),166)

    def test_independent_controller_formulas_and_submission_bounds(self):
        cfg = json.loads((ROOT / "configs/core.json").read_text(encoding="utf-8-sig"))
        ctl = cfg["control"]
        archive = ROOT / "verification/g1_speed_revision_20261021"
        for scheme in ("G0", "G1", "G2", "G3"):
            for v in ("lead", "follow"):
                previous = ctl["b_initial_hz"]
                count = 0
                for row in rows(archive/scheme/f"control_log_{v}.csv", "W r b Cbar"):
                    # Archived logs predate the warmup gate: replay them with warmup=0.
                    previous = audit_controller(row, scheme, ctl, float(row["Cbar"]), previous, 0.0)
                    count += 1
                self.assertEqual(count,299)
        last = dict(row)
        last["W"] = "2.9"
        with self.assertRaisesRegex(ValueError,"controller W"):
            audit_controller(last,"G3",ctl,float(last["Cbar"]),previous,0.0)
        gated = dict(last)
        gated["W"] = row["W"]
        gated["tick_time"] = "10.0"
        self.assertEqual(
            audit_controller(gated,"G3",ctl,float(last["Cbar"]),float(last["b"]),20.0),
            float(last["b"]), "no integration before warmup")
        clock=Clock(cfg["timing"])
        with tempfile.TemporaryDirectory() as d:
            logs=Path(d)
            life={"v":(0,clock.tick(1.3),2,0,False)}
            cfg["data_origin"]="simulation"
            def check(times):
                write(logs/"tx_log.csv","sender_id seq t_submit",[("v",i,t) for i,t in enumerate(times)])
                return tx_metrics(logs,clock,cfg,life,{"v"},{},(0,clock.tick(1.3)),clock.tick(1),clock.tick(.1))
            self.assertEqual(check([.1,1.1])["v"][0],2)
            with self.assertRaisesRegex(ValueError,"frequency limits"):
                check([.09,1.09])
            with self.assertRaisesRegex(ValueError,"frequency limits"):
                check([.1,1.11])

    def test_h2_no_exposure_preserves_full_and_post_windows(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            cfg=self.build(root,"S1")
            result=analyze_state(root,cfg,{"earliest_t0_s":.3})
            h=result["h2"]["p"]
            self.assertEqual(h["active_status"],"insufficient_mechanism_exposure")
            self.assertEqual(h["active"]["exposure_s"],0)
            self.assertIsNone(h["active"]["mean_abs_error_m"])
            self.assertIsNone(h["active"]["mean_r_hz"])
            self.assertAlmostEqual(h["full"]["exposure_s"],.35)
            self.assertAlmostEqual(h["post"]["exposure_s"],.15)
            self.assertEqual(h["full"]["saturated_fraction"],1)

    def test_s2_recovery_boundaries(self):
        clock=Clock({"time_resolution_s":.1})
        kw=dict(start=60,stop=70,warm=60,endpoint=100,confirm=10,target=.60,ceiling=.63,clock=clock)
        def r(segments):
            return recovery(segments,**kw)
        a=r([(10,60,.63),(60,70,.8),(70,79,.63),(79,80,.7),(80,100,.63)])
        self.assertEqual(a["status"],"recovered")
        self.assertAlmostEqual(a["delay_s"],1)
        self.assertAlmostEqual(a["confirmation_s"],9)
        self.assertAlmostEqual(a["above_ceiling_s"],1.1)
        self.assertAlmostEqual(a["above_target_s"],4)
        self.assertEqual(r([(10,100,.63)])["status"],"never_exceeded")
        self.assertEqual(r([(10,60,.63),(60,91,.7),(91,100,.61)])["status"],"not_recovered_by_end")
        self.assertEqual(r([(10,58,.60),(58,60,.7),(60,100,.60)])["status"],"pre_baseline_not_eligible")
        with self.assertRaisesRegex(ValueError,"gap/overlap"):
            r([(10,60,.63),(61,100,.60)])
        with self.assertRaisesRegex(ValueError,"uncovered"):
            group_series({"v":[(10,0,.62,1,1),(12,0,.62,1,1)]},["v"],10,13,1,2,False)

    def test_real_terminal_timer_requires_its_own_normal_trip_evidence(self):
        cfg = json.loads((ROOT/"configs/core.json").read_text(encoding="utf-8-sig"))
        cfg.update(scheme="G0", data_origin="simulation")
        cfg["timing"]["warmup_s"] = 0  # fixture predates the warmup gate
        clock = Clock(cfg["timing"])
        tick = clock.tick
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            logs = root/"logs"
            logs.mkdir()
            (logs/"sumo_diagnostics.json").write_text(json.dumps({"raw_evidence_paths":{"trips.xml":"trips.xml"}}))
            (root/"trips.xml").write_text('<tripinfos><tripinfo id="exit" arrival="0.2"/>'
                '<tripinfo id="keep" arrival="0.2" vaporized="true"/></tripinfos>')
            fields = "vehicle_id tick_time window_start window_end busy_duration_s C Cbar r W e_H B S U b speed_mps"
            life = {v:(0,tick(.3),0,0,True) for v in ("keep","exit")}
            life["brief"] = (tick(.24),tick(.25),0,0,True)
            write(logs/"busy_audit.csv", "vehicle_id transition_time busy_state", [("keep",0,0),("exit",0,0),("brief",.24,0)])
            control = []
            for n in (1,2,3):
                for v in ("keep","exit"):
                    if v=="exit" and n==3:
                        continue
                    base = 2 + n*.3
                    control.append((v,n/10,(n-1)/10,n/10,0,0,0,base,1,0,0,0,0,base,0))
            def run(records):
                write(logs/"control_log.csv",fields,records)
                return aggregate_controls(logs,clock,cfg,life,{},(0,tick(.3)),tick(.1),.8)
            _,vehicles,audit = run(control)
            self.assertEqual(audit["final_control_cancelled_on_normal_arrival"],["exit"])
            self.assertIsNone(vehicles["brief"]["mean_cbr"])
            with self.assertRaisesRegex(ValueError,"Truncated terminal control trace for keep"):
                run(control[:-1])
            with self.assertRaisesRegex(ValueError,"Truncated terminal control trace for exit"):
                run([r for r in control if not (r[0]=="exit" and r[1]==.2)])

if __name__=="__main__":
    unittest.main()
