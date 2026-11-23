"""Gated bridge to the original, unchanged manage.run implementation."""
import argparse
import json
from pathlib import Path
import sys
import traceback


def main():
    if sys.stdin.buffer.readline() != b"GO\n":
        raise RuntimeError("Engine was not released by its owning Windows Job Object")
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--tools", required=True)
    parser.add_argument("--scheme", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--result-file", required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.project) / "tools"))
    import manage
    try:
        result = manage.run(args.tools, args.scheme, args.seed, False)
        Path(args.result_file).write_text(json.dumps({"result_dir": result, "success": True}), encoding="utf-8")
    except Exception as error:
        Path(args.result_file).write_text(json.dumps({"success": False, "error": str(error)}), encoding="utf-8")
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
