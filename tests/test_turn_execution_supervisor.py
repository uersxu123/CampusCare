from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from app.services.execution_control import ExecutionBudget
from app.services.process_supervisor import ProcessSupervisor


def _return_value(payload, _cancel_event):
    return {"value": payload["value"]}


def _hang_forever(_payload, _cancel_event):
    while True:
        pass


def _crash_worker(_payload, _cancel_event):
    os._exit(23)


def _late_success(_payload, cancel_event):
    while not cancel_event.is_set():
        time.sleep(0.005)
    return {"late": True}


def _spawn_descendant_and_hang(payload, _cancel_event):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path(payload["pidFile"]).write_text(str(child.pid), encoding="utf-8")
    while True:
        time.sleep(0.05)


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        process = kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        kernel32.CloseHandle(wintypes.HANDLE(process))
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_execution_budget_uses_one_absolute_deadline_with_fake_clock() -> None:
    now = [10.0]
    budget = ExecutionBudget.start(5.0, clock=lambda: now[0])
    assert budget.remaining() == 5.0
    now[0] = 13.5
    assert budget.remaining() == 1.5
    assert budget.bounded_timeout(10.0) == 1.5
    now[0] = 15.0
    assert budget.expired()
    assert budget.bounded_timeout(3.0) == 0.0


def test_supervisor_completes_normal_worker() -> None:
    result = ProcessSupervisor(timeout_seconds=2).run(_return_value, {"value": 7})
    assert result.status == "COMPLETED"
    assert result.value == {"value": 7}
    assert result.resource_cleanup == "CONFIRMED"


def test_supervisor_hard_terminates_permanent_python_loop() -> None:
    started = time.monotonic()
    result = ProcessSupervisor(
        timeout_seconds=0.2,
        cancel_grace_seconds=0.05,
        cleanup_timeout_seconds=1.0,
    ).run(_hang_forever, {})
    assert time.monotonic() - started < 3.0
    assert result.status == "TIMED_OUT"
    assert result.cooperative_cancel_requested is True
    assert result.terminated or result.killed
    assert result.resource_cleanup == "CONFIRMED"
    assert result.provider_cancellation == "UNKNOWN"


def test_worker_crash_is_terminal_and_not_replayed() -> None:
    result = ProcessSupervisor(timeout_seconds=2).run(_crash_worker, {})
    assert result.status == "CRASHED"
    assert result.diagnostics["exitCode"] == 23


def test_client_cancel_is_interrupted_and_not_replayed() -> None:
    started = time.monotonic()
    result = ProcessSupervisor(
        timeout_seconds=2,
        cancel_grace_seconds=0.02,
        cleanup_timeout_seconds=1.0,
    ).run(
        _hang_forever,
        {},
        generation_id="cancelled-generation",
        cancel_requested=lambda: time.monotonic() - started >= 0.1,
    )
    assert result.status == "INTERRUPTED"
    assert result.value is None
    assert result.cooperative_cancel_requested is True
    assert result.resource_cleanup == "CONFIRMED"


def test_late_success_cannot_overwrite_timeout() -> None:
    result = ProcessSupervisor(
        timeout_seconds=0.15,
        cancel_grace_seconds=0.2,
        cleanup_timeout_seconds=1.0,
    ).run(_late_success, {})
    assert result.status == "TIMED_OUT"
    assert result.value is None


def test_windows_job_reclaims_descendant_process(tmp_path) -> None:
    pid_file = tmp_path / "descendant.pid"
    result = ProcessSupervisor(
        timeout_seconds=0.4,
        cancel_grace_seconds=0.05,
        cleanup_timeout_seconds=1.0,
    ).run(_spawn_descendant_and_hang, {"pidFile": str(pid_file)})
    assert result.status == "TIMED_OUT"
    assert pid_file.exists()
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    for _ in range(40):
        if not _pid_exists(child_pid):
            break
        time.sleep(0.05)
    if os.name == "nt":
        assert result.job_assigned is True, result.diagnostics
    assert not _pid_exists(child_pid)


def test_faulted_case_does_not_pollute_next_synthetic_case() -> None:
    supervisor = ProcessSupervisor(timeout_seconds=0.15, cancel_grace_seconds=0.02)
    failed = supervisor.run(_hang_forever, {}, generation_id="case-a")
    completed = ProcessSupervisor(timeout_seconds=2).run(
        _return_value,
        {"value": "case-b"},
        generation_id="case-b",
    )
    assert failed.status == "TIMED_OUT"
    assert completed.status == "COMPLETED"
    assert completed.value == {"value": "case-b"}
