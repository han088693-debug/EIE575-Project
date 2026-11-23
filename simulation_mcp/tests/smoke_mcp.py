"""Real MCP acceptance: successful G0/G3 batch, reconnect, and owned cancellation.

This script starts real S1 simulations. It only cancels job IDs it creates.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import uuid

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from client import connect, describe_error, result_data


TERMINAL = {"passed", "failed", "cancelled", "interrupted"}
EXPECTED_TOOLS = {"start_run", "get_status", "tail_log", "stop_run", "get_results"}


async def call(session, name, arguments=None):
    return result_data(await session.call_tool(name, arguments or {}))


async def expect_error(session, name, arguments):
    result = await session.call_tool(name, arguments)
    assert result.isError, f"{name} accepted invalid input: {arguments}"
    return "\n".join(
        block.text for block in result.content if getattr(block, "type", None) == "text"
    )


async def wait_terminal(session, job_id, timeout_s):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = await call(session, "get_status", {"job_id": job_id})
        if status["status"] in TERMINAL:
            return status
        await asyncio.sleep(0.2)
    raise TimeoutError(f"Task did not finish within {timeout_s} s: {job_id}")


def identity_alive(identity):
    try:
        proc = psutil.Process(identity["pid"])
        return abs(proc.create_time() - identity["create_time"]) < 0.01 and proc.is_running()
    except psutil.NoSuchProcess:
        return False


async def run_smoke(evidence, timeout_s):
    created_jobs = []
    request_id = "mcp_smoke_" + uuid.uuid4().hex
    try:
        async with connect() as session:
            listed = (await session.list_tools()).tools
            names = {tool.name for tool in listed}
            assert names == EXPECTED_TOOLS, names
            assert all(tool.outputSchema and tool.outputSchema.get("type") == "object" for tool in listed)
            evidence["tool_names"] = sorted(names)
            evidence["structured_output_schemas"] = True
            initial = await call(session, "get_status")
            evidence["environment"] = initial["environment"]
            assert initial["environment"]["ready"], initial["environment"]
            evidence["invalid_scheme"] = await expect_error(session, "start_run", {"schemes": ["G9"]})
            evidence["boolean_seed"] = await expect_error(session, "start_run", {"seeds": [True]})
            evidence["invalid_job_id"] = await expect_error(session, "get_status", {"job_id": "../outside"})

            batch_args = {"schemes": ["G0", "G3"], "seeds": [0], "request_id": request_id}
            started = await call(session, "start_run", batch_args)
            job_id = started["job_id"]
            created_jobs.append(job_id)
            evidence["batch_job_id"] = job_id
            repeated = await call(session, "start_run", batch_args)
            assert repeated["job_id"] == job_id
            evidence["idempotent_retry"] = True
            evidence["request_conflict"] = await expect_error(
                session, "start_run", {**batch_args, "schemes": ["G1"]}
            )
            evidence["concurrent_start"] = await expect_error(
                session, "start_run", {"schemes": ["G2"], "seeds": [2]}
            )
            before_disconnect = await call(session, "get_status", {"job_id": job_id})
            assert before_disconnect["status"] not in TERMINAL, before_disconnect
            evidence["status_before_disconnect"] = before_disconnect

        # The same task must survive the first server/client connection closing.
        async with connect() as session:
            reconnected = await call(session, "get_status", {"job_id": job_id})
            assert reconnected["job_id"] == job_id
            assert reconnected["status"] not in {"interrupted", "failed", "cancelled"}, reconnected
            evidence["status_after_reconnect"] = reconnected
            finished = await wait_terminal(session, job_id, timeout_s)
            evidence["batch_final_status"] = finished
            assert finished["status"] == "passed", finished
            assert finished["completed_runs"] == 2, finished
            logs = await call(session, "tail_log", {"job_id": job_id, "lines": 12, "run_index": 0})
            evidence["sample_log"] = logs
            assert logs["available"] and logs["text"], logs
            assert len(logs["text"].splitlines()) <= 12, logs
            evidence["invalid_log_path"] = await expect_error(
                session, "tail_log", {"job_id": job_id, "log": "../job.json"}
            )
            results = await call(session, "get_results", {"job_id": job_id})
            evidence["batch_results"] = results
            assert results["complete"] is True and len(results["runs"]) == 2, results
            for item in results["runs"]:
                assert item["status"] == "passed" and item["complete"] is True, item
                summary = item["summary"]
                assert summary and summary["passed"] is True, item
                assert summary["checks_total"] > 0 and summary["failed_checks"] == [], item

            cancelled_job = await call(session, "start_run", {
                "schemes": ["G0"], "seeds": [1], "request_id": request_id + "_cancel"
            })
            cancel_id = cancelled_job["job_id"]
            created_jobs.append(cancel_id)
            evidence["cancel_job_id"] = cancel_id
            deadline = time.monotonic() + timeout_s
            identities = []
            while time.monotonic() < deadline:
                active = await call(session, "get_status", {"job_id": cancel_id})
                assert active["status"] not in TERMINAL, active
                process_names = {proc["name"].lower() for proc in active["process_tree"]}
                if {"eie575_tabr_starter.exe", "sumo.exe"} <= process_names:
                    identities = active["process_tree"]
                    evidence["observed_engine_before_stop"] = active
                    break
                await asyncio.sleep(0.05)
            assert identities, "Both real OMNeT++ and SUMO engines were not observed before the timeout"
            evidence["stop_response"] = await call(session, "stop_run", {"job_id": cancel_id})
            stopped = await wait_terminal(session, cancel_id, 15)
            evidence["cancel_final_status"] = stopped
            assert stopped["status"] == "cancelled", stopped
            partial = await call(session, "get_results", {"job_id": cancel_id})
            evidence["cancel_results"] = partial
            assert partial["complete"] is False, partial
            assert all(item["complete"] is False for item in partial["runs"]), partial
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and any(identity_alive(item) for item in identities):
                await asyncio.sleep(0.1)
            remaining = [item for item in identities if identity_alive(item)]
            assert not remaining, remaining
            evidence["observed_processes_terminated"] = identities
            repeated_stop = await call(session, "stop_run", {"job_id": cancel_id})
            assert repeated_stop["status"] == "cancelled", repeated_stop
            evidence["idempotent_stop"] = True

        evidence["passed"] = True
    finally:
        # Cleanup must never touch pre-existing or unrelated tasks.
        if created_jobs:
            async with connect() as session:
                for owned_id in created_jobs:
                    current = await call(session, "get_status", {"job_id": owned_id})
                    if current["status"] not in TERMINAL:
                        await call(session, "stop_run", {"job_id": owned_id})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=120, help="Maximum wall seconds per phase")
    parser.add_argument("--evidence", type=Path, default=ROOT / "evidence" / "mcp_smoke.json")
    args = parser.parse_args()
    if not 10 <= args.timeout <= 3600:
        parser.error("--timeout must be between 10 and 3600 seconds")
    evidence = {"started_at": datetime.now(timezone.utc).isoformat(), "passed": False}
    started = time.monotonic()
    exit_code = 0
    try:
        asyncio.run(run_smoke(evidence, args.timeout))
    except Exception as error:
        evidence["error"] = describe_error(error)
        exit_code = 1
    finally:
        evidence["elapsed_s"] = round(time.monotonic() - started, 3)
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": evidence["passed"], "evidence": str(args.evidence),
                      "elapsed_s": evidence["elapsed_s"], "error": evidence.get("error")}, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
