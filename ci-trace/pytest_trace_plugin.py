"""Log each test's time window and worker PID, so eBPF file opens can be attributed to tests.

Enabled with ``-p script.trace.pytest_trace_plugin`` and ``TRACE_DIR``. Every pytest
process (controller and xdist workers) appends JSON lines to
``$TRACE_DIR/tests-<pid>.jsonl``:

    {"nodeid": ..., "pid": ..., "start_ns": ..., "end_ns": ..., "outcome": ...}

Times come from ``time.monotonic_ns()``, the same CLOCK_MONOTONIC that bpftrace's
``nsecs`` uses, so both logs share one clock. The window covers setup, call and
teardown, because fixtures open files too.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import pytest

_outcomes: dict[str, str] = {}
_handle: Any = None


def _out():
    global _handle  # noqa: PLW0603
    if _handle is None:
        trace_dir = os.environ.get("TRACE_DIR", ".")
        os.makedirs(trace_dir, exist_ok=True)
        _handle = open(  # noqa: SIM115
            os.path.join(trace_dir, f"tests-{os.getpid()}.jsonl"), "a", encoding="utf-8"
        )
    return _handle


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    start = time.monotonic_ns()
    yield
    end = time.monotonic_ns()
    out = _out()
    out.write(
        json.dumps(
            {
                "nodeid": item.nodeid,
                "pid": os.getpid(),
                "start_ns": start,
                "end_ns": end,
                "outcome": _outcomes.pop(item.nodeid, "unknown"),
            }
        )
        + "\n"
    )
    out.flush()


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    # Keep the worst outcome across setup/call/teardown.
    if report.failed:
        _outcomes[report.nodeid] = "failed"
    elif report.skipped and _outcomes.get(report.nodeid) != "failed":
        _outcomes[report.nodeid] = "skipped"
    elif report.when == "call" and report.nodeid not in _outcomes:
        _outcomes[report.nodeid] = "passed"
