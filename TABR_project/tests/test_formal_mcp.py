"""Exercise real MCP submit/reconnect/result and owned-process cancellation using a synthetic batch."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import psutil

PROJECT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((PROJECT / "configs/core.json").read_text(encoding="utf-8-sig"))
APP = Path(CONFIG["tools"]["mcp_dir"])
SERVER = PROJECT / "tools/formal_mcp.py"
sys.path.insert(0, str(APP))
from client import connect, result_data
from controller import read_json
from process_control import process_alive


async def protocol(root):
    queue = root / "queue"
    batches = root / "results/batches"
    batch = batches / "example"
    os.environ["EIE575_FORMAL_QUEUE"] = str(queue)
    os.environ["EIE575_FORMAL_BATCH_ROOT"] = str(batches)
    cfg = dict(CONFIG, stage="synthetic", fixture_case="prior_only")
    cfg["timing"] = dict(cfg["timing"],duration_s=17,warmup_s=5,stationary_eval_s=10)
    cfg["geometry"] = dict(cfg["geometry"],background_pair_count=1)
    config = root / "config.json"
    config.write_text(json.dumps(cfg, ensure_ascii=False),encoding="utf-8")
    subprocess.run([CONFIG["tools"]["analysis_python"], "-B", "-X", "utf8", str(PROJECT/"tools/experiment.py"),
                    "plan","--config",str(config),"--output",str(batch),"--scope","synthetic"],
                   check=True, capture_output=True)
    env = os.environ.copy()
    with (root/"host.log").open("wb") as log:
        host = subprocess.Popen([sys.executable,"-B","-X","utf8",str(SERVER),"--serve"], env=env,
                                stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            for _ in range(100):
                if (queue/"host.json").is_file():
                    break
                if host.poll() is not None:
                    raise RuntimeError("Test host exited")
                await asyncio.sleep(.1)
            async with connect(SERVER) as session:
                names = sorted(t.name for t in (await session.list_tools()).tools)
                assert set(names) == {"start_run","start_probe","get_status","get_results","tail_log","stop_run"}, names
                assert result_data(await session.call_tool("get_status", {}))["host"]["ready"]
                bad = await session.call_tool("start_run", dict(batch="../invalid",job="S0_low_G0_seed0",request_id="bad"))
                assert bad.isError
                run = dict(batch="example",job="S0_low_G0_seed0",request_id="reconnect",resume=True)
                first = result_data(await session.call_tool("start_run", run))
            # The first client and its stdio server have now closed.
            async with connect(SERVER) as session:
                async def call(name, **kwargs):
                    return result_data(await session.call_tool(name, kwargs))
                second = await call("start_run", **run)
                assert first["created_at"] == second["created_at"]
                for _ in range(180):
                    state = await call("get_status",request_id="reconnect")
                    if state["state"] in {"completed","failed","interrupted"}:
                        break
                    await asyncio.sleep(.25)
                tail = await call("tail_log", request_id="reconnect",lines=20)
                assert state["state"] == "completed", (state,tail)
                result = await call("get_results",request_id="reconnect")
                assert result["complete"] and result["scope"] == "synthetic" and result["metrics"]
                await call("start_run",batch="example",job="S1_low_G2_seed0",request_id="cancel")
                for _ in range(400):
                    row = await call("get_status",request_id="cancel")
                    if row.get("engine_alive"):
                        break
                    await asyncio.sleep(.025)
                assert row.get("engine_alive"), row
                child = row["engine"]
                await call("stop_run",request_id="cancel")
                for _ in range(90):
                    final = await call("get_status",request_id="cancel")
                    if final["state"] == "cancelled":
                        break
                    await asyncio.sleep(.1)
                assert final["state"] == "cancelled" and not process_alive(child), final
                assert final["host"]["ready"] and not (await call("get_results",request_id="cancel"))["complete"]
                return dict(passed=True,scope="synthetic",tools=names,request_after_reconnect=state["state"],
                            repeat_request_deduplicated=True,invalid_batch_rejected=True,
                            valid_attempt_id=result["attempt_id"],cancel_state=final["state"],owned_process_alive=False)
        finally:
            if host.poll() is None:
                for child in psutil.Process(host.pid).children(recursive=True):
                    try:
                        child.terminate()
                    except psutil.NoSuchProcess:
                        pass
                if host.poll() is None:
                    host.terminate()
            host.wait(timeout=10)


class FormalProtocolTest(unittest.TestCase):
    def test_protocol(self):
        with tempfile.TemporaryDirectory(prefix="tabr_mcp_test_") as tmp:
            result=asyncio.run(protocol(Path(tmp)))
            self.assertTrue(result["passed"])


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence",type=Path,help="New external evidence directory")
    args=parser.parse_args()
    if args.evidence:
        args.evidence.mkdir(parents=True,exist_ok=False)
        report=asyncio.run(protocol(args.evidence.resolve()))
        (args.evidence/"report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
        print(json.dumps(report))
    else:
        unittest.main(argv=[sys.argv[0]])
