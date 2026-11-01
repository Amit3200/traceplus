"""Decorator timing, span nesting, and exception tests."""

from __future__ import annotations

import asyncio

import pytest

from traceplus import get_parent_span_id, get_span_id, get_trace_id, set_trace_context, tracked


@tracked
def charge_user() -> str:
    return "ok"


@tracked
def boom() -> None:
    raise RuntimeError("boom")


@tracked
async def aload() -> int:
    return 5


@tracked(log_entry=True)
def entry_fn() -> int:
    return 1


class Box:
    @tracked
    @staticmethod
    def top() -> str:
        return "top"

    @staticmethod
    @tracked
    def bottom() -> str:
        return "bottom"

    @classmethod
    @tracked
    def fetch(cls) -> str:
        return cls.__name__

    @tracked
    @classmethod
    def load(cls) -> str:
        return "load"


def test_tracked_sync_logs_duration_and_function(logs, monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter((5.0, 5.125))
    monkeypatch.setattr("traceplus.decorators.time.perf_counter", lambda: next(ticks))
    assert charge_user() == "ok"
    payload = logs.records()[-1]
    assert payload["message"] == "Executed charge_user"
    assert payload["function"] == "charge_user"
    assert payload["module"] == charge_user.__wrapped__.__module__  # type: ignore[attr-defined]
    assert payload["line"] == charge_user.__wrapped__.__code__.co_firstlineno  # type: ignore[attr-defined]
    assert payload["duration_ms"] == 125.0
    assert payload["level"] == "INFO"
    assert payload["trace_id"]
    assert get_trace_id() == ""


def test_tracked_reraises_and_logs_one_json_line(logs) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        boom()
    line = logs.lines()[-1]
    assert "\n" not in line
    payload = logs.records()[-1]
    assert payload["level"] == "ERROR"
    assert "boom" in payload["message"]
    assert "RuntimeError" in payload["exception"]
    assert isinstance(payload["duration_ms"], (int, float))
    assert get_trace_id() == ""


def test_tracked_async(logs) -> None:
    assert asyncio.run(aload()) == 5
    payload = logs.records()[-1]
    assert payload["function"] == "aload"
    assert payload["message"] == "Executed aload"
    assert "duration_ms" in payload


def test_log_entry_emits_two_lines(logs) -> None:
    assert entry_fn() == 1
    entering, executed = logs.records()
    assert entering["message"] == "Entering entry_fn"
    assert "duration_ms" not in entering
    assert executed["message"] == "Executed entry_fn"
    assert "duration_ms" in executed


def test_nested_spans_restore_the_parent(logs) -> None:
    set_trace_context("root-trace", "rootspanrootspan")
    seen: dict[str, str] = {}

    @tracked
    def outer() -> None:
        seen["outer"] = get_span_id()
        inner()
        seen["after"] = get_span_id()

    @tracked
    def inner() -> None:
        seen["inner"] = get_span_id()
        seen["parent"] = get_parent_span_id()
        assert get_trace_id() == "root-trace"

    outer()
    assert seen["outer"] != "rootspanrootspan"
    assert seen["inner"] != seen["outer"]
    assert seen["parent"] == seen["outer"]
    assert seen["after"] == seen["outer"]
    assert get_trace_id() == "root-trace"
    assert get_span_id() == "rootspanrootspan"
    assert get_parent_span_id() == ""


def test_staticmethod_and_classmethod_orders(logs) -> None:
    assert Box.top() == "top"
    assert Box.bottom() == "bottom"
    assert Box.fetch() == "Box"
    assert Box.load() == "load"
    functions = [record["function"] for record in logs.records()]
    assert functions == ["Box.top", "Box.bottom", "Box.fetch", "Box.load"]


def test_unknown_decorator_level_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown log level"):

        @tracked(level="NOPE")
        def rejected() -> None:
            return None
