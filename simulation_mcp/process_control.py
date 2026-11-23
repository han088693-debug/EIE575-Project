"""Windows-only ownership and cleanup for a gated simulation process tree.

The child command MUST block on stdin until it receives ``GO\n``. That gate
lets us put it in a kill-on-close Windows Job Object before it can create any
simulation descendants. There is deliberately no unmanaged-process fallback.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import subprocess
import threading
from typing import BinaryIO, Mapping

import psutil


def process_alive(identity: Mapping | None) -> bool:
    """Test a specific process incarnation, rather than a possibly reused PID."""
    if not identity or type(identity.get("pid")) is not int:
        return False
    if identity["pid"] <= 0:
        return False
    try:
        expected = float(identity["create_time"])
        process = psutil.Process(identity["pid"])
        return (
            abs(process.create_time() - expected) < 0.001
            and process.is_running()
            and process.status() != psutil.STATUS_ZOMBIE
        )
    except (KeyError, TypeError, ValueError, psutil.Error):
        return False


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _windows_api():
    if os.name != "nt":
        raise RuntimeError("Simulation process ownership currently requires Windows.")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    api.CreateJobObjectW.restype = wintypes.HANDLE
    api.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    api.SetInformationJobObject.restype = wintypes.BOOL
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    api.AssignProcessToJobObject.restype = wintypes.BOOL
    api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.TerminateJobObject.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


def _checked(result, operation: str):
    if not result:
        code = ctypes.get_last_error()
        raise OSError(code, f"{operation}: {ctypes.FormatError(code).strip()}")
    return result


class OwnedProcess:
    """A gated child and all its descendants, owned by one Windows Job Object.

    The output file remains owned by the caller. ``close()`` is idempotent and
    kills any descendants left behind even if the immediate child has exited.
    If the Python owner crashes, Windows closes the non-inherited job handle
    and applies the same cleanup automatically.
    """

    def __init__(
        self,
        command: list[str],
        cwd: os.PathLike | str,
        env: dict[str, str],
        output: BinaryIO,
    ):
        if not isinstance(command, list) or not command or not all(
            isinstance(item, str) for item in command
        ):
            raise ValueError("command must be a nonempty list of strings")
        self._api = _windows_api()
        self._lock = threading.RLock()
        self._job = None
        self.process = None
        self.identity = None
        try:
            self._job = _checked(
                self._api.CreateJobObjectW(None, None), "CreateJobObjectW"
            )
            limits = _ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
            _checked(
                self._api.SetInformationJobObject(
                    self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                ),
                "SetInformationJobObject",
            )
            self.process = subprocess.Popen(
                command,
                cwd=os.fspath(cwd),
                env=env,
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.STDOUT,
                close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self.identity = {
                "pid": self.process.pid,
                "create_time": psutil.Process(self.process.pid).create_time(),
            }
            # PROCESS_SET_QUOTA | PROCESS_TERMINATE, required by AssignProcessToJobObject.
            handle = _checked(
                self._api.OpenProcess(0x0100 | 0x0001, False, self.process.pid),
                "OpenProcess",
            )
            try:
                _checked(
                    self._api.AssignProcessToJobObject(self._job, handle),
                    "AssignProcessToJobObject",
                )
            finally:
                self._api.CloseHandle(handle)
            self.process.stdin.write(b"GO\n")
            self.process.stdin.flush()
            self.process.stdin.close()
        except BaseException:
            # A not-yet-assigned shim is still behind the gate and has no children.
            if self.process is not None:
                if self.process.stdin is not None and not self.process.stdin.closed:
                    self.process.stdin.close()
                if self.process.poll() is None:
                    self.process.kill()
                self.process.wait(timeout=10)
            self.close()
            raise

    def poll(self) -> int | None:
        return self.process.poll()

    def stop(self) -> int:
        """Terminate only this owned tree, including orphaned descendants."""
        with self._lock:
            if self._job is not None:
                _checked(self._api.TerminateJobObject(self._job, 130), "TerminateJobObject")
            return self.process.wait(timeout=10)

    def close(self) -> None:
        with self._lock:
            if self._job is not None:
                handle, self._job = self._job, None
                _checked(self._api.CloseHandle(handle), "CloseHandle(job)")
            if self.process is not None:
                self.process.wait(timeout=10)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
