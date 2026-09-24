from __future__ import annotations

import multiprocessing
import os
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


ProcessWorker = Callable[[dict[str, Any], Any], Any]


@dataclass(frozen=True)
class SupervisedProcessResult:
    generation_id: str
    status: str
    value: Any = None
    error_code: str = ""
    error_message: str = ""
    elapsed_seconds: float = 0.0
    worker_pid: int | None = None
    cooperative_cancel_requested: bool = False
    terminated: bool = False
    killed: bool = False
    job_assigned: bool = False
    resource_cleanup: str = "UNAVAILABLE"
    provider_cancellation: str = "UNKNOWN"
    diagnostics: dict[str, Any] = field(default_factory=dict)


class ProcessSupervisor:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        cancel_grace_seconds: float = 0.25,
        cleanup_timeout_seconds: float = 2.0,
        poll_interval_seconds: float = 0.01,
    ):
        self.timeout_seconds = max(0.01, float(timeout_seconds))
        self.cancel_grace_seconds = max(0.0, float(cancel_grace_seconds))
        self.cleanup_timeout_seconds = max(0.1, float(cleanup_timeout_seconds))
        self.poll_interval_seconds = max(0.001, float(poll_interval_seconds))

    def run(
        self,
        worker: ProcessWorker,
        payload: dict[str, Any],
        *,
        generation_id: str | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> SupervisedProcessResult:
        generation = generation_id or uuid.uuid4().hex
        context = multiprocessing.get_context("spawn")
        parent_connection, child_connection = context.Pipe(duplex=False)
        start_gate = context.Event()
        cancel_event = context.Event()
        process = context.Process(
            target=_supervised_entry,
            args=(worker, payload, generation, child_connection, start_gate, cancel_event),
            name=f"mindbridge-worker-{generation[:8]}",
        )
        started = time.monotonic()
        process.start()
        child_connection.close()
        job_handle, job_assigned, job_error = _assign_kill_on_close_job(process)
        start_gate.set()
        deadline = started + self.timeout_seconds
        terminal_status = ""
        envelope = None
        cooperative_cancel = False
        try:
            while time.monotonic() < deadline:
                if parent_connection.poll(self.poll_interval_seconds):
                    try:
                        candidate = parent_connection.recv()
                    except EOFError:
                        terminal_status = "CRASHED"
                        break
                    if candidate.get("generationId") == generation:
                        envelope = candidate
                        terminal_status = str(candidate.get("status") or "CRASHED")
                        break
                if cancel_requested is not None and cancel_requested():
                    cooperative_cancel = True
                    cancel_event.set()
                    terminal_status = "INTERRUPTED"
                    break
                if not process.is_alive():
                    terminal_status = "CRASHED"
                    break
            if not terminal_status:
                cooperative_cancel = True
                cancel_event.set()
                terminal_status = "TIMED_OUT"

            if terminal_status in {"TIMED_OUT", "INTERRUPTED"} and process.is_alive():
                process.join(self.cancel_grace_seconds)
            elif process.is_alive():
                process.join(self.cleanup_timeout_seconds)

            terminated = False
            killed = False
            if process.is_alive():
                process.terminate()
                terminated = True
                process.join(self.cleanup_timeout_seconds)
            if process.is_alive():
                process.kill()
                killed = True
                process.join(self.cleanup_timeout_seconds)
            exited = not process.is_alive()
        finally:
            parent_connection.close()
            _close_job(job_handle)

        elapsed = max(0.0, time.monotonic() - started)
        if envelope is not None and terminal_status == "COMPLETED":
            value = envelope.get("value")
            error_code = ""
            error_message = ""
        else:
            value = None
            error_code = str((envelope or {}).get("errorCode") or terminal_status)
            error_message = str((envelope or {}).get("errorMessage") or "")
        cleanup = "CONFIRMED" if exited and (job_assigned or os.name != "nt") else "PARTIAL"
        return SupervisedProcessResult(
            generation_id=generation,
            status=terminal_status,
            value=value,
            error_code=error_code,
            error_message=error_message,
            elapsed_seconds=elapsed,
            worker_pid=process.pid,
            cooperative_cancel_requested=cooperative_cancel,
            terminated=terminated,
            killed=killed,
            job_assigned=job_assigned,
            resource_cleanup=cleanup,
            diagnostics={
                "exitCode": process.exitcode,
                "jobError": job_error,
                "stackCapture": "UNAVAILABLE",
            },
        )


def _supervised_entry(worker, payload, generation_id, connection, start_gate, cancel_event) -> None:
    try:
        if not start_gate.wait(30.0):
            return
        value = worker(payload, cancel_event)
        connection.send({"generationId": generation_id, "status": "COMPLETED", "value": value})
    except BaseException as exc:
        try:
            connection.send({
                "generationId": generation_id,
                "status": "CRASHED",
                "errorCode": type(exc).__name__,
                "errorMessage": str(exc)[:500],
                "traceback": traceback.format_exc(limit=20)[-8000:],
            })
        except Exception:
            pass
    finally:
        connection.close()


def _assign_kill_on_close_job(process) -> tuple[Any, bool, str]:
    if os.name != "nt":
        return None, True, ""
    try:
        import ctypes
        from ctypes import wintypes

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
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

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
        process_handle = int(process._popen._handle)
        if not kernel32.AssignProcessToJobObject(handle, wintypes.HANDLE(process_handle)):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        return handle, True, ""
    except Exception as exc:
        return None, False, f"{type(exc).__name__}: {exc}"


def _close_job(handle) -> None:
    if handle is None or os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle(handle)
    except Exception:
        pass
