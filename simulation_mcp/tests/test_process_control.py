"""Actual Windows process-tree tests; no simulation engine is started."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from process_control import OwnedProcess, process_alive


SHIM = r'''
import json, os, pathlib, subprocess, sys, time
assert sys.stdin.readline() == "GO\n"
child = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(60)"],
                         creationflags=subprocess.CREATE_NO_WINDOW)
pathlib.Path(sys.argv[1]).write_text(json.dumps([os.getpid(), child.pid]))
time.sleep(60)
'''


def wait_until(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition did not become true")


def read_pids(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def cleanup_pids(pids):
    for pid in pids:
        try:
            process = psutil.Process(pid)
            process.kill()
            process.wait(timeout=5)
        except psutil.NoSuchProcess:
            pass


@unittest.skipUnless(os.name == "nt", "Windows Job Object tests")
class ProcessControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eie575_process_test_")
        self.root = Path(self.tmp.name)
        self.shim = self.root / "shim.py"
        self.shim.write_text(SHIM)
        self.pids_file = self.root / "pids.json"
        self.output = (self.root / "output.log").open("wb")
        self.owned = None
        self.worker = None
        self.spectator = subprocess.Popen(
            [sys.executable, "-B", "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def tearDown(self):
        try:
            if self.owned:
                self.owned.close()
            if self.worker:
                if self.worker.poll() is None:
                    self.worker.kill()
                self.worker.wait(timeout=5)
            cleanup_pids(read_pids(self.pids_file) or [])
            self.spectator.kill()
            self.spectator.wait(timeout=5)
        finally:
            self.output.close()
            self.tmp.cleanup()

    def make_owned(self):
        self.owned = OwnedProcess(
            [sys.executable, "-B", str(self.shim), str(self.pids_file)],
            self.root, os.environ.copy(), self.output,
        )
        wait_until(lambda: read_pids(self.pids_file))
        return read_pids(self.pids_file)

    def assert_stopped(self, pids):
        wait_until(lambda: all(not psutil.pid_exists(pid) for pid in pids))
        self.assertIsNone(self.spectator.poll(), "unowned process was affected")

    def test_stop_cleans_tree_without_touching_spectator(self):
        pids = self.make_owned()
        self.assertTrue(process_alive(self.owned.identity))
        self.assertFalse(process_alive({**self.owned.identity, "create_time": 1.0}))
        self.assertEqual(self.owned.stop(), 130)
        self.assert_stopped(pids)
        self.assertFalse(process_alive(self.owned.identity))
        self.owned.close()
        self.owned.close()

    def test_close_cleans_tree(self):
        pids = self.make_owned()
        self.owned.close()
        self.assert_stopped(pids)

    def test_crashed_owner_cleans_tree(self):
        owner = self.root / "owner.py"
        owner.write_text(
            "import os, sys, time\n"
            f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
            "from process_control import OwnedProcess\n"
            f"output = open({str(self.root / 'owner.log')!r}, 'wb')\n"
            f"owned = OwnedProcess([sys.executable, '-B', {str(self.shim)!r}, "
            f"{str(self.pids_file)!r}], {str(self.root)!r}, os.environ.copy(), output)\n"
            "time.sleep(60)\n"
        )
        self.worker = subprocess.Popen(
            [sys.executable, "-B", str(owner)],
            stdout=self.output, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        wait_until(lambda: read_pids(self.pids_file))
        pids = read_pids(self.pids_file)
        self.worker.kill()
        self.worker.wait(timeout=5)
        self.assert_stopped(pids)

    def test_process_identity_validation(self):
        for identity in (None, {}, {"pid": -1}, {"pid": "1"}, {"pid": True}):
            self.assertFalse(process_alive(identity))

    def test_bad_command_does_not_launch(self):
        with self.assertRaises(ValueError):
            OwnedProcess([], self.root, os.environ.copy(), self.output)
        with self.assertRaises(FileNotFoundError):
            OwnedProcess([str(self.root / "missing.exe")], self.root, os.environ.copy(), self.output)
        self.assertIsNone(self.spectator.poll())


if __name__ == "__main__":
    unittest.main()
