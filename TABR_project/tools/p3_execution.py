"""P3 frozen runtime constraints, imported by the formal runner's snapshot and gate."""


def effective_policy(cfg, job):
    policy = cfg.get("execution", {})
    limits = policy.get("wall_timeout_by_stratum_s")
    if limits is None:
        return policy
    from experiment import SCENARIO_LOADS

    expected = {s + "_" + l for s, loads in SCENARIO_LOADS for l in loads}
    if set(limits) != expected or any(type(v) is not int or v <= 0 for v in limits.values()):
        raise ValueError("Incomplete or invalid P3 wall-time limits")
    return dict(policy, wall_timeout_s=limits[job["scenario"] + "_" + job["load_id"]])


def verify_policy(cfg, receipt, project):
    import experiment as ex

    policy = cfg.get("execution", {})
    if "wall_timeout_by_stratum_s" not in policy and "execution_policy_sha256" not in receipt:
        return
    if receipt.get("execution_policy_sha256") != ex.identity(policy):
        raise ValueError("Frozen execution policy differs from P3 evidence")
    effective_policy(cfg, {"scenario": "S1", "load_id": "high"})
    if receipt.get("simulation_sources") != ex.simulation_sources(project) or receipt.get(
        "runtime_hashes"
    ) != ex.runtime_hashes(cfg):
        raise ValueError("Current simulator or runtime differs from its P3 acceptance")
