"""List, run and monitor pre-registered engineering probes through the TABR MCP host."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import uuid

PROJECT = Path(__file__).resolve().parents[1]
SERVER = PROJECT / "tools/formal_mcp.py"
CORE = json.loads((PROJECT / "configs/core.json").read_text(encoding="utf-8-sig"))
sys.path.insert(0, CORE["tools"]["mcp_dir"])
from client import connect, result_data
from controller import save_json

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


async def invoke(args):
    if args.action == "list":
        registry = json.loads((PROJECT / "configs/formal_probes.json").read_text(encoding="utf-8-sig"))["probes"]
        emit({"registered": len(registry), "probes": registry})
        return 0
    async with connect(SERVER) as session:
        async def call(tool, **values):
            return result_data(await session.call_tool(tool, values))
        if args.action == "status":
            emit(await call("get_status", **({"request_id": args.request} if args.request else {})))
            return 0
        if args.action == "stop":
            emit(await call("stop_run", request_id=args.request))
            return 0
        if args.action == "run":
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            request = args.request_id or ("r_" + stamp + "_" + uuid.uuid4().hex[:12])
            value = await call("start_probe", probe=args.probe, request_id=request)
            emit({"request": request, "probe": value["probe"], "state": value["state"]})
        else:
            request = args.request
        receipt = args.receipt or PROJECT / "verification/probe_receipts" / (request + ".json")
        start = time.monotonic()
        previous = None
        while True:
            status = await call("get_status", request_id=request)
            state = status["state"]
            if state != previous:
                emit({"request": request, "state": state, "job": status["job"], "engine": status.get("engine")})
                previous = state
            if state in TERMINAL:
                result = await call("get_results", request_id=request)
                report = {"request": request, "status": status, "result": result,
                          "driver_log_tail": await call("tail_log",request_id=request,log="driver",lines=30)}
                diagnostic = None
                if not result["complete"]:
                    diagnostic = {}
                    if result.get("attempt"):
                        for log in ("simulation", "launchd", "engine"):
                            detail = await call("tail_log", request_id=request, log=log, lines=15)
                            if detail["available"]:
                                diagnostic[log] = detail["text"]
                    if report["driver_log_tail"]["text"]:
                        diagnostic["driver"] = report["driver_log_tail"]["text"]
                    report["diagnostics"] = diagnostic
                receipt.parent.mkdir(parents=True, exist_ok=True)
                save_json(receipt, report)
                emit({"request": request, "complete": result["complete"], "scope": result["scope"],
                      "attempt": result.get("attempt"), "receipt": str(receipt.resolve()),
                      "metrics": result.get("metrics"), "error": result.get("attempt_error") or result.get("error"),
                      "diagnostic": diagnostic})
                if result["complete"]:
                    return 0
                return int(status.get("exit_code") or (130 if state == "cancelled" else 40))
            if args.timeout and time.monotonic() - start >= args.timeout:
                emit({"request": request, "state": state, "wait_timed_out": True, "retry": "wait " + request})
                return 40
            await asyncio.sleep(args.poll)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    subs.add_parser("list", help="List the registered engineering probes and effective parameter references")
    status = subs.add_parser("status", help="Query the MCP host or a single request")
    status.add_argument("request", nargs="?")
    stop = subs.add_parser("stop", help="Request cancellation of this request's process tree")
    stop.add_argument("request")
    run = subs.add_parser("run", help="Submit one registered probe and wait for completion")
    run.add_argument("probe")
    run.add_argument("--request-id", help="Use the same ID to resume watching the same submission")
    wait = subs.add_parser("wait", help="Reconnect to an existing request and write its final receipt")
    wait.add_argument("request")
    for sub in (run,wait):
        sub.add_argument("--poll", type=float, default=2, help="Seconds between status queries")
        sub.add_argument("--timeout", type=float, default=0, help="Stop waiting after this many seconds (0 = no limit)")
        sub.add_argument("--receipt", type=Path, help="Result JSON path (default verification/probe_receipts/REQUEST.json)")
    args=parser.parse_args()
    if args.action in ("run","wait") and (not 0.1 <= args.poll <= 60 or args.timeout < 0):
        parser.error("--poll must be 0.1–60 seconds and --timeout must be nonnegative")
    try:
        return asyncio.run(invoke(args))
    except (RuntimeError, KeyError, ValueError, OSError) as exc:
        emit({"error": str(exc)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
