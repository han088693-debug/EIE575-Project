"""Attempt-bound wall-clock supervision. Operator grants and commands live outside the scientific configuration."""

import argparse
import csv
import json
import math
import os
from pathlib import Path
import re
import time

import psutil
from experiment import file_hash, json_read, locked, save, write_atomic
from timeout_recovery import last_complete_tick

PROJECT = Path(__file__).resolve().parents[1]
REQUEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")


def require(value, why):
    if not value:
        raise ValueError(why)


def seconds(value):
    require(
        type(value) in (int, float) and math.isfinite(value) and value > 0,
        "Expected a positive finite time budget",
    )
    return value


def directory(project, request):
    require(isinstance(request, str) and REQUEST.fullmatch(request), "Invalid request ID")
    return project / "verification/wall_runtime" / request


def match_request(row):
    return {k: row[k] for k in ("request_id", "job_id", "scope", "registration_sha256", "physics_sha256")}


def write_once(path, value):
    if path.exists():
        require(json_read(path) == value, "Existing budget record differs")
    else:
        save(path, value)


def register_grant(project, approval, expected_sha, request, *, benchmarks=None, benchmark_sha=None):
    """Import an explicit operator approval. No MCP tool can mint or modify a grant."""
    import hashlib

    raw = Path(approval).read_bytes()
    require(hashlib.sha256(raw).hexdigest() == expected_sha, "Operator approval hash mismatch")
    auth = json.loads(raw)
    require(
        auth.get("format") == "eie575-wall-approval-v1" and auth.get("approved") is True,
        "Missing explicit operator approval",
    )
    matches = [r for r in auth["requests"] if r["request_id"] == request]
    require(len(matches) == 1, "Approval does not name this individual request")
    row = matches[0]
    require(
        row["scope"] in ("engineering", "formal", "pilot")
        and row["job_id"]
        and all(re.fullmatch("[0-9a-f]{64}", row[k]) for k in ("registration_sha256", "physics_sha256")),
        "Incomplete registered job binding",
    )
    cap = seconds(row["authorized_cap_s"])
    mode = row["initial_mode"]
    require(
        mode in ("full_cap", "evidence") and type(row["continue_on_progress"]) is bool,
        "Unknown approval policy",
    )
    evidence = {}
    if mode == "evidence":
        from initial_wall_budget import select_initial_budget
        from e0_budget_benchmarks import pinned_e0_benchmarks

        require(
            benchmarks and benchmark_sha and benchmark_sha == row["benchmark_sha256"],
            "Pinned benchmark dataset required",
        )
        args = row["benchmark_context"]
        require(
            args["machine_id"] == os.environ.get("COMPUTERNAME"), "Benchmark machine differs from this host"
        )
        verified = pinned_e0_benchmarks(
            benchmarks, benchmark_sha, project=project, machine_id=args["machine_id"]
        )
        evidence = select_initial_budget(verified, authorized_cap_s=cap, **args)
        initial = seconds(evidence["wall_timeout_s"])
    else:
        initial = cap
    chosen = seconds(row.get("initial_limit_s", initial))
    require(
        chosen <= cap and (mode != "evidence" or chosen >= initial),
        "Initial deadline violates benchmark or approved cap",
    )
    grant = {
        **match_request(row),
        "approval_sha256": expected_sha,
        "authorized_cap_s": cap,
        "initial_limit_s": chosen,
        "continue_on_progress": row["continue_on_progress"],
        "recommendation": evidence,
    }
    root = directory(project, request)
    with locked(root):
        require(
            not (root / "binding.json").exists() or (root / "grant.json").exists(),
            "Cannot mint a grant for an already bound request",
        )
        path = root / "approval.json"
        if path.exists():
            require(path.read_bytes() == raw, "Conflicting archived authority")
        else:
            write_atomic(path, raw)
        write_once(root / "grant.json", grant)
    return grant


def observe_ticks(logs, run_id):
    values = []
    for name, field in (("control_log.csv", "tick_time"), ("scenario_truth.csv", "t")):
        try:
            value = last_complete_tick(logs / name, field, run_id)
            values.append(value if value is not None and math.isfinite(value) else None)
        except (OSError, UnicodeError, ValueError, csv.Error, IndexError, StopIteration):
            values.append(None)
    return values


class Supervisor:
    def __init__(
        self,
        project,
        attempt,
        configured,
        request=None,
        registration=None,
        *,
        started=None,
        inspect_s=1800,
        interval_s=15,
        stall_s=120,
    ):
        self.attempt = Path(attempt)
        self.logs = self.attempt / "logs"
        m = json_read(self.attempt / "run_manifest.json")
        require(
            m["attempt_id"] == m["run_id"] == self.attempt.name and m["status"] == "running",
            "Cannot supervise a different or closed attempt",
        )
        request = request or m["attempt_id"]
        self.root = directory(project, request)
        self.binding = dict(
            request_id=request,
            job_id=m["job_id"],
            scope=m["scope"],
            registration_sha256=registration or m["physics_sha256"],
            attempt_id=m["attempt_id"],
            physics_sha256=m["physics_sha256"],
            run_id=m["run_id"],
            simulation_job_key=m["simulation_job_key"],
        )
        self.started = time.monotonic() if started is None else started
        self.soft = seconds(inspect_s)
        self.interval = seconds(interval_s)
        self.stall = seconds(stall_s)
        self.next_review = 0
        self.next_soft = self.soft
        self.revision = 0
        self.last_tick = None
        self.last_cpu = None
        self.last_progress = 0
        self.last_cpu_motion = 0
        self.observed_progress = False
        self.health = "awaiting_evidence"
        self.last_error = None
        self.warned = False
        self.active = True
        self.auto = False
        self.cap = self.limit = seconds(configured)
        with locked(self.root):
            path = self.root / "grant.json"
            if path.exists():
                g = json_read(path)
                proof = self.root / "approval.json"
                require(file_hash(proof) == g["approval_sha256"], "Approval evidence changed")
                original = [r for r in json_read(proof)["requests"] if r["request_id"] == request]
                require(
                    len(original) == 1
                    and match_request(original[0]) == match_request(g)
                    and original[0]["authorized_cap_s"] == g["authorized_cap_s"]
                    and original[0]["continue_on_progress"] is g["continue_on_progress"],
                    "Changed approval terms",
                )
                approved = original[0]
                if approved["initial_mode"] == "evidence":
                    snapshot = json_read(attempt / "config_snapshot.json")
                    context = approved["benchmark_context"]
                    require(
                        snapshot["density_per_lane_km"] == context["density"]
                        and snapshot["timing"]["duration_s"] == context["duration_s"]
                        and "/".join(
                            [snapshot["scenario"], snapshot["scheme"], snapshot["observation_mode"]]
                        )
                        == context["cost_group"],
                        "Budget evidence refers to another workload",
                    )
                proposed = approved.get(
                    "initial_limit_s", g["recommendation"].get("wall_timeout_s", g["authorized_cap_s"])
                )
                require(g["initial_limit_s"] == proposed, "Changed initial execution allowance")
                require(
                    match_request(g) == match_request(self.binding),
                    "Grant belongs to another scientific request",
                )
                self.cap = seconds(g["authorized_cap_s"])
                require(
                    self.cap >= self.limit,
                    "Approval below the registered execution budget; reconcile before starting",
                )
                self.limit = max(self.limit, seconds(g["initial_limit_s"]))
                require(
                    self.limit <= self.cap and type(g["continue_on_progress"]) is bool,
                    "Invalid initial grant",
                )
                self.auto = g["continue_on_progress"]
                self.grant_sha = file_hash(path)
            else:
                self.grant_sha = None
            binding_path = self.root / "binding.json"
            require(not binding_path.exists(), "Request already bound to an engine invocation")
            save(binding_path, dict(self.binding, attempt=str(self.attempt), grant_sha256=self.grant_sha))
        self.owner_created = psutil.Process().create_time()
        estimate = g["recommendation"] if self.grant_sha else {}
        self.record(
            "started",
            configured_s=configured,
            soft_review_s=self.soft,
            binding=self.binding,
            grant_sha256=self.grant_sha,
            authority=(
                "operator_request_grant" if self.grant_sha else "registered_execution_policy_or_default"
            ),
            recommendation=estimate,
            cap_reason="explicit_request_ceiling" if estimate.get("benchmark_exceeds_cap") else None,
            approval_sha256=g["approval_sha256"] if self.grant_sha else None,
        )
        self.state()

    def state(self):
        data = dict(
            self.binding,
            active=self.active,
            current_s=self.limit,
            ceiling_s=self.cap,
            started_monotonic=self.started,
            controller_pid=os.getpid(),
            controller_created=self.owner_created,
            revision=self.revision,
            progress=self.health,
            approaching_hard_ceiling=self.warned,
            grant_sha256=self.grant_sha,
        )
        for retry in range(8):
            try:
                save(self.logs / "wall_budget_state.json", data)
                return
            except PermissionError as exc:
                if getattr(exc, "winerror", None) not in (5, 32, 33) or retry == 7:
                    raise
                time.sleep(0.03 * (retry + 1))

    def record(self, event, **detail):
        with (self.logs / "wall_budget.jsonl").open("a", encoding="utf8") as f:
            f.write(
                json.dumps(
                    dict(
                        event=event,
                        utc_epoch=time.time(),
                        wall_s=time.monotonic() - self.started,
                        current_s=self.limit,
                        ceiling_s=self.cap,
                        **detail,
                    ),
                    allow_nan=False,
                )
                + "\n"
            )
            f.flush()
            os.fsync(f.fileno())

    def review(self, elapsed, ticks, cpu):
        valid = all(v is not None for v in ticks)
        advancing = False
        if valid and self.last_tick is not None:
            if not all(a >= b for a, b in zip(ticks, self.last_tick)):
                self.health = "progress_clock_error"
                self.record("monitor_error", ticks=ticks, last_ticks=self.last_tick)
                self.next_review = elapsed + self.interval
                return
            advancing = all(a > b for a, b in zip(ticks, self.last_tick))
        cpu_busy = self.last_cpu is not None and cpu > self.last_cpu + 0.005
        if advancing:
            self.last_progress = elapsed
            self.observed_progress = True
        if cpu_busy:
            self.last_cpu_motion = elapsed
        if not valid:
            self.health = "evidence_unavailable"
        elif advancing:
            self.health = "advancing"
        elif elapsed - self.last_progress >= self.stall and elapsed - self.last_cpu_motion >= self.stall:
            self.health = "no_observed_progress"
        elif cpu_busy:
            self.health = "cpu_active_buffering_possible"
        else:
            self.health = "awaiting_evidence"
        if valid:
            self.last_tick = ticks
        self.last_cpu = cpu
        self.next_review = elapsed + self.interval

    def update(self, elapsed):
        path = self.root / "updates" / f"{self.revision+1:06d}.json"
        if not path.exists():
            return
        try:
            r = json_read(path)
            require(
                self.grant_sha is not None
                and r["binding"] == self.binding
                and r["grant_sha256"] == self.grant_sha,
                "Foreign approval binding",
            )
            value = seconds(r["new_limit_s"])
            require(
                r["revision"] == self.revision + 1
                and self.limit < value <= self.cap
                and self.started <= r["published_monotonic"] < self.started + self.limit,
                "Expired, decreasing or excessive update",
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            message = str(exc)
            if self.last_error != message:
                self.record("update_rejected", error=message)
                self.last_error = message
            return
        self.limit = value
        self.revision += 1
        self.record("operator_extension", revision=self.revision, command_sha256=file_hash(path))
        self.state()

    def check(self, cpu, now=None):
        elapsed = (time.monotonic() if now is None else now) - self.started
        if elapsed >= self.cap:
            self.record("hard_timeout")
            raise TimeoutError("Per-job wall timeout")
        if not self.warned and elapsed >= self.cap - min(300, self.cap * 0.15):
            self.warned = True
            self.record("approaching_hard_ceiling", remaining_s=self.cap - elapsed, progress=self.health)
            self.state()
        self.update(elapsed)
        if elapsed >= self.next_review or elapsed >= self.next_soft or elapsed >= self.limit:
            ticks = observe_ticks(self.logs, self.binding["run_id"])
            self.review(elapsed, ticks, cpu)
            if elapsed >= self.next_soft:
                self.record("soft_review", progress=self.health, ticks=ticks, cpu_s=cpu)
                self.next_soft = elapsed + self.soft
            self.state()
        if elapsed >= self.limit:
            progressing = (
                self.health == "advancing"
                or self.health == "cpu_active_buffering_possible"
                and self.observed_progress
                and elapsed - self.last_progress <= self.stall
            )
            if self.auto and progressing and self.limit < self.cap:
                self.limit = self.cap
                self.record("preauthorized_extension", progress=self.health)
                self.state()
            else:
                self.record("execution_deadline", progress=self.health)
                raise TimeoutError("Per-job wall timeout")

    def close(self):
        self.active = False
        self.state()
        self.record("closed")


def extend(project, request, attempt_id, job_key, new_s, reason):
    root = directory(project, request)
    with locked(root):
        row = json_read(root / "binding.json")
        grant = json_read(root / "grant.json")
        require(
            row["attempt_id"] == attempt_id
            and row["simulation_job_key"] == job_key
            and row["request_id"] == request,
            "Request, attempt or scientific job identity mismatch",
        )
        binding = {k: v for k, v in row.items() if k not in ("attempt", "grant_sha256")}
        require(
            match_request(grant) == match_request(binding)
            and file_hash(root / "grant.json") == row["grant_sha256"],
            "Changed or foreign grant",
        )
        state = json_read(Path(row["attempt"]) / "logs/wall_budget_state.json")
        require(
            state["active"]
            and all(state[k] == v for k, v in binding.items())
            and state["grant_sha256"] == row["grant_sha256"],
            "Attempt no longer active",
        )
        try:
            owner = psutil.Process(state["controller_pid"])
            require(abs(owner.create_time() - state["controller_created"]) < 0.001, "Controller PID reused")
        except psutil.NoSuchProcess as exc:
            raise ValueError("Budget controller has exited") from exc
        next_rev = state["revision"] + 1
        require(
            state["current_s"] < seconds(new_s) <= state["ceiling_s"] == grant["authorized_cap_s"],
            "Update must increase the budget within its sealed ceiling",
        )
        now = time.monotonic()
        require(
            now < state["started_monotonic"] + state["current_s"]
            and isinstance(reason, str)
            and reason.strip(),
            "Expired update or missing reason",
        )
        path = root / "updates" / f"{next_rev:06d}.json"
        require(not path.exists(), "Previous command is pending")
        save(
            path,
            dict(
                binding=binding,
                revision=next_rev,
                new_limit_s=new_s,
                published_monotonic=now,
                grant_sha256=row["grant_sha256"],
                reason=reason,
            ),
        )
        return {"published": True, "revision": next_rev, "request_id": request, "attempt_id": attempt_id}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    g = sub.add_parser("register")
    g.add_argument("--approval", type=Path, required=True)
    g.add_argument("--sha256", required=True)
    g.add_argument("--benchmarks", type=Path)
    g.add_argument("--benchmark-sha256")
    x = sub.add_parser("extend")
    x.add_argument("--attempt", required=True)
    x.add_argument("--job-key", required=True)
    x.add_argument("--seconds", type=int, required=True)
    x.add_argument("--reason", required=True)
    for cmd in (g, x):
        cmd.add_argument("--request", required=True)
    args = p.parse_args()
    try:
        result = (
            register_grant(
                PROJECT,
                args.approval,
                args.sha256,
                args.request,
                benchmarks=args.benchmarks,
                benchmark_sha=args.benchmark_sha256,
            )
            if args.action == "register"
            else extend(PROJECT, args.request, args.attempt, args.job_key, args.seconds, args.reason)
        )
        print(json.dumps(result))
    except (OSError, ValueError, KeyError) as exc:
        p.exit(2, str(exc) + "\n")
