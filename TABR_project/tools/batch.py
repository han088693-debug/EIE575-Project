from __future__ import print_function

import argparse
import datetime
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import manage


def save(path, batch):
    with open(path, "w") as handle:
        json.dump(batch, handle, indent=2, sort_keys=True)


def main():
    parser = argparse.ArgumentParser(description="Serial S1 functional batch; not the formal 114-run matrix.")
    parser.add_argument("--tools", default="E:/EIE575_Tools")
    parser.add_argument("--schemes", nargs="+", choices=["G0", "G1", "G2", "G3"],
                        default=["G0", "G1", "G2", "G3"])
    parser.add_argument("--seeds", nargs="+", type=int, default=[0, 1, 2, 3, 4])
    args = parser.parse_args()
    if len(set(args.schemes)) != len(args.schemes) or len(set(args.seeds)) != len(args.seeds):
        parser.error("Schemes and seeds must not contain duplicates.")
    if any(seed < 0 for seed in args.seeds):
        parser.error("Seeds must be non-negative.")
    try:
        manage.ROOT.encode("ascii")
        args.tools.encode("ascii")
    except UnicodeError:
        parser.error("Use the ASCII working folder under E:/EIE575_Tools/workspace.")
    batch_id = "S1_smoke_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    directory = os.path.join(manage.ROOT, "results", "batches", batch_id)
    os.makedirs(directory)
    manifest = os.path.join(directory, "batch.json")
    jobs = [{"scheme": scheme, "seed": seed, "status": "pending",
             "elapsed_s": None, "result_dir": None}
            for seed in args.seeds for scheme in args.schemes]
    batch = {"batch_id": batch_id, "scope": "S1 two-vehicle 30-second functional tests",
             "formal_114_runs": False, "execution": "serial", "status": "running",
             "elapsed_s": 0, "jobs": jobs}
    save(manifest, batch)
    started = time.time()
    for index, job in enumerate(jobs):
        job["status"] = "running"
        save(manifest, batch)
        print("[%d/%d] %s seed=%d" % (index + 1, len(jobs), job["scheme"], job["seed"]))
        sys.stdout.flush()
        run_started = time.time()
        try:
            job["result_dir"] = manage.run(args.tools, job["scheme"], job["seed"], False)
            job["status"] = "passed"
        except Exception as error:
            job["status"] = "failed"
            job["error"] = str(error)
            batch["status"] = "failed"
            raise
        finally:
            job["elapsed_s"] = time.time() - run_started
            batch["elapsed_s"] = time.time() - started
            save(manifest, batch)
        average = sum(item["elapsed_s"] for item in jobs[:index + 1]) / (index + 1)
        remaining = average * (len(jobs) - index - 1)
        print("Passed in %.2f s; estimated remaining %.1f s" % (job["elapsed_s"], remaining))
        sys.stdout.flush()
    batch["status"] = "passed"
    batch["elapsed_s"] = time.time() - started
    save(manifest, batch)
    print("Batch passed: %d/%d runs in %.2f s" % (len(jobs), len(jobs), batch["elapsed_s"]))
    print("Batch manifest:", manifest)


if __name__ == "__main__":
    main()
