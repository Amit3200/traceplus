"""Context, schema, and logger tests."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import uuid
from datetime import datetime, timezone

import pytest

from traceplus import (
    TracePulseContextFilter,
    TracePulseJSONFormatter,
    __version__,
    apply_to_handler,
    bind,
    configure,
    get_logger,
    get_parent_span_id,
    get_span_id,
    get_trace_id,
    incoming_trace,
    inject_headers,
    set_trace_context,
    span,
    trace,
)
from traceplus.core import begin_trace, reset_config, reset_trace_context, resolve_incoming

TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


def _emit_marker() -> None:
    get_logger("marker").info("marker")


def test_version_is_set() -> None:
    assert __version__ == "0.1.0"


def test_public_api_imports() -> None:
    import traceplus
    import traceplus.middleware.django
    import traceplus.middleware.fastapi
    import traceplus.middleware.flask

    for name in (
        "configure",
        "get_logger",
        "tracked",
        "trace",
        "span",
        "bind",
        "instrument",
        "inject_headers",
        "incoming_trace",
    ):
        assert hasattr(traceplus, name)
    assert traceplus.middleware.fastapi.TracePulseMiddleware.__bases__ == (object,)
    assert traceplus.middleware.flask.TracePulseMiddleware.__bases__ == (object,)
    assert traceplus.middleware.django.TracePulseMiddleware.__bases__ == (object,)


def test_formatter_schema_and_timestamp() -> None:
    record = logging.LogRecord("billing.api", logging.INFO, __file__, 12, "fetched", (), None)
    record.created = datetime(2026, 11, 1, 17, 20, tzinfo=timezone.utc).timestamp()
    set_trace_context("a1b2c3d4-e5f6-7890-abcd-ef0123456789", "f9e8d7c6b5a44321")
    payload = json.loads(TracePulseJSONFormatter().format(record))
    assert payload["timestamp"] == "2026-11-01T17:20:00.000Z"
    assert TIMESTAMP.fullmatch(payload["timestamp"])
    assert payload["level"] == "INFO"
    assert payload["message"] == "fetched"
    assert payload["trace_id"] == "a1b2c3d4-e5f6-7890-abcd-ef0123456789"
    assert payload["span_id"] == "f9e8d7c6b5a44321"
    assert payload["logger"] == "billing.api"
    assert payload["environment"] == "development"
    assert "extra" not in payload
    assert "parent_span_id" not in payload


def test_environment_and_service_from_existing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEPULSE_ENVIRONMENT", "staging")
    monkeypatch.setenv("TRACEPULSE_SERVICE", "orders")
    reset_config()
    record = logging.LogRecord("orders.api", logging.INFO, __file__, 10, "ok", (), None)
    payload = json.loads(TracePulseJSONFormatter().format(record))
    assert payload["environment"] == "staging"
    assert payload["service"] == "orders"


def test_configure_overrides_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEPULSE_ENVIRONMENT", "staging")
    configure(service="billing", environment="production", emit=False)
    record = logging.LogRecord("billing.api", logging.INFO, __file__, 10, "ok", (), None)
    payload = json.loads(TracePulseJSONFormatter().format(record))
    assert payload["environment"] == "production"
    assert payload["service"] == "billing"


def test_service_from_the_host_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "TRACEPULSE_SERVICE",
        "SERVICE_NAME",
        "K_SERVICE",
        "GAE_SERVICE",
        "AWS_LAMBDA_FUNCTION_NAME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "orders")
    reset_config()
    record = logging.LogRecord("app", logging.INFO, __file__, 1, "ok", (), None)
    payload = json.loads(TracePulseJSONFormatter().format(record))
    assert payload["service"] == "orders"

    monkeypatch.setenv("K_SERVICE", "payments")
    payload = json.loads(TracePulseJSONFormatter().format(record))
    assert payload["service"] == "payments"


def test_default_environment_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("TRACEPULSE_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
    reset_config()
    record = logging.LogRecord("app", logging.INFO, __file__, 10, "ok", (), None)
    payload = json.loads(TracePulseJSONFormatter().format(record))
    assert payload["environment"] == "development"
    assert "service" not in payload


def test_context_resets_to_the_previous_trace() -> None:
    set_trace_context("outer", "s-outer", "p-outer")
    tokens = set_trace_context("inner", "s-inner", "p-inner")
    assert get_trace_id() == "inner"
    assert get_span_id() == "s-inner"
    assert get_parent_span_id() == "p-inner"
    reset_trace_context(tokens)
    assert get_trace_id() == "outer"
    assert get_span_id() == "s-outer"
    assert get_parent_span_id() == "p-outer"


def test_ids_are_single_line_and_capped() -> None:
    set_trace_context("abc\nFORGED\r" + ("x" * 300), "span\r\n1")
    assert "\n" not in get_trace_id()
    assert "\r" not in get_trace_id()
    assert len(get_trace_id()) == 256
    assert get_span_id() == "span1"


def test_trace_span_and_bind_context_managers() -> None:
    with trace("root-trace", span_id="parentparentpare") as trace_id:
        assert trace_id == "root-trace"
        with span() as child:
            assert get_parent_span_id() == "parentparentpare"
            assert child != "parentparentpare"
            assert get_trace_id() == "root-trace"
        assert get_span_id() == "parentparentpare"
    assert get_trace_id() == ""

    with span() as created:
        assert created
        assert get_trace_id()
    assert get_trace_id() == ""


def test_incoming_trace_resets_after_an_error() -> None:
    with pytest.raises(RuntimeError, match="stop"):
        with incoming_trace({"X-Request-ID": "in-1"}) as trace_id:
            assert trace_id == "in-1"
            assert get_trace_id() == "in-1"
            raise RuntimeError("stop")
    assert get_trace_id() == ""


def test_resolve_prefers_configured_header_and_keeps_matching_parent() -> None:
    mismatched = {
        "X-Request-ID": "app-id",
        "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
    }
    trace_id, parent = resolve_incoming(mismatched, "X-Request-ID")
    assert trace_id == "app-id"
    assert parent == ""

    dashed = "a1b2c3d4-e5f6-7890-abcd-ef0123456789"
    set_trace_context(dashed, "f9e8d7c6b5a44321")
    outgoing = inject_headers()
    tokens, incoming, new_span = begin_trace(outgoing)
    try:
        assert incoming == dashed
        assert get_parent_span_id() == "f9e8d7c6b5a44321"
        assert new_span != "f9e8d7c6b5a44321"
        assert outgoing["traceparent"].startswith("00-a1b2c3d4e5f67890abcdef0123456789-f9e8d7c6b5a44321-01")
    finally:
        reset_trace_context(tokens)


def test_resolve_traceparent_xray_and_correlation_id() -> None:
    trace_id, parent = resolve_incoming(
        {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"},
        "X-Request-ID",
    )
    assert trace_id == "0af7651916cd43dd8448eb211c80319c"
    assert parent == "b7ad6b7169203331"

    trace_id, parent = resolve_incoming(
        {"X-Amzn-Trace-Id": "Root=1-67891233-abcdef0123456789abcdef01;Parent=53995c3f42cd8ad8;Sampled=0"},
        "X-Request-ID",
    )
    assert trace_id == "1-67891233-abcdef0123456789abcdef01"
    assert parent == "53995c3f42cd8ad8"

    trace_id, parent = resolve_incoming({"X-Correlation-ID": "corr-7"}, "X-Request-ID")
    assert trace_id == "corr-7"
    assert parent == ""

    generated, parent = resolve_incoming({}, "X-Request-ID")
    uuid.UUID(generated)
    assert parent == ""


def test_inject_headers_keeps_caller_values_and_skips_invalid_traceparent() -> None:
    set_trace_context("not-a-trace", "span")
    headers = inject_headers({"Accept": "application/json", "X-Request-ID": "keep"})
    assert headers["Accept"] == "application/json"
    assert headers["X-Request-ID"] == "keep"
    assert "traceparent" not in headers
    assert headers["X-Span-ID"] == "span"


def test_async_tasks_keep_separate_traces() -> None:
    async def worker(trace_id: str) -> str:
        set_trace_context(trace_id, "s")
        await asyncio.sleep(0)
        return get_trace_id()

    async def main() -> tuple[str, str]:
        return tuple(await asyncio.gather(worker("a"), worker("b")))

    assert asyncio.run(main()) == ("a", "b")


def test_threads_keep_separate_traces() -> None:
    barrier = threading.Barrier(2)
    found: dict[str, str] = {}

    def worker(trace_id: str) -> None:
        set_trace_context(trace_id, "s")
        barrier.wait(timeout=2)
        found[trace_id] = get_trace_id()

    threads = [threading.Thread(target=worker, args=(trace_id,)) for trace_id in ("t-one", "t-two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert found == {"t-one": "t-one", "t-two": "t-two"}


def test_logger_reports_the_caller_and_promotes_fields(logs) -> None:
    _emit_marker()
    payload = logs.records()[-1]
    assert payload["message"] == "marker"
    assert payload["function"] == "_emit_marker"
    assert payload["module"] == __name__
    assert payload["service"] == "billing"
    assert payload["environment"] == "test"
    assert "extra" not in payload

    get_logger("billing.api").info(
        "charged",
        extra={"name": "widget", "user_id": 7, "duration_ms": 12.5},
    )
    payload = logs.records()[-1]
    assert payload["duration_ms"] == 12.5
    assert payload["extra"] == {"name": "widget", "user_id": 7}
    assert payload["logger"] == "billing.api"


def test_bind_merges_and_does_not_leak(logs) -> None:
    with bind(user_id=9):
        with bind(action="fetch"):
            get_logger("billing.api").info("inner", extra={"user_id": 3})
        get_logger("billing.api").info("outer")
    get_logger("billing.api").info("after")
    inner, outer, after = logs.records()
    assert inner["extra"] == {"user_id": 3, "action": "fetch"}
    assert outer["extra"] == {"user_id": 9}
    assert "extra" not in after


def test_nonserializable_values_stay_on_one_json_line(logs) -> None:
    moment = datetime(2026, 11, 1, 17, 20, tzinfo=timezone.utc)
    identifier = uuid.uuid4()
    loop: list[object] = []
    loop.append(loop)
    get_logger("billing.api").info("values", extra={"at": moment, "user_id": identifier, "tags": {"a", "b"}})
    get_logger("billing.api").info("loop", extra={"loop": loop})
    lines = logs.lines()
    assert all("\n" not in line for line in lines)
    first, second = logs.records()
    assert first["extra"]["at"] == "2026-11-01T17:20:00.000Z"
    assert first["extra"]["user_id"] == str(identifier)
    assert sorted(first["extra"]["tags"]) == ["a", "b"]
    assert second["message"] == "loop"
    assert "extra" in second


def test_unicode_is_preserved(logs) -> None:
    get_logger("billing.api").info("café")
    assert "café" in logs.lines()[-1]


def test_stamped_record_survives_another_thread(logs) -> None:
    with bind(user_id=4):
        set_trace_context("queued-trace", "span1234span1234", "parentspanparen")
        record = logging.LogRecord("queue", logging.INFO, __file__, 1, "hello", (), None)
        assert TracePulseContextFilter().filter(record) is True
    record.trace_id = "queued-trace"
    configure(service="other", environment="other", emit=False)
    payload = json.loads(TracePulseJSONFormatter().format(record))
    assert payload["trace_id"] == "queued-trace"
    assert payload["span_id"] == "span1234span1234"
    assert payload["parent_span_id"] == "parentspanparen"
    assert payload["extra"]["user_id"] == 4
    assert payload["service"] == "billing"
    assert payload["environment"] == "test"


def test_configure_is_idempotent(logs) -> None:
    root = logging.getLogger()
    assert len([handler for handler in root.handlers if getattr(handler, "_traceplus", False)]) == 1
    configure(service="renamed", environment="prod")
    assert len([handler for handler in root.handlers if getattr(handler, "_traceplus", False)]) == 1
    get_logger("app").info("hello")
    payload = logs.records()[-1]
    assert payload["service"] == "renamed"
    assert payload["environment"] == "prod"


def test_configure_emit_false_does_not_add_a_handler() -> None:
    configure(service="s", environment="e", emit=False)
    assert not any(getattr(handler, "_traceplus", False) for handler in logging.getLogger().handlers)


def test_apply_to_an_existing_handler() -> None:
    import io

    name = "traceplus.tests.existing"
    logger = logging.getLogger(name)
    previous = (list(logger.handlers), logger.propagate, logger.level)
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    apply_to_handler(handler)
    apply_to_handler(handler)
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        set_trace_context("t-1", "abcdabcdabcdabcd")
        logger.info("hello", extra={"user_id": 7, "duration_ms": 3})
    finally:
        logger.handlers, logger.propagate, logger.level = previous
    payload = json.loads(buffer.getvalue())
    assert payload["trace_id"] == "t-1"
    assert payload["message"] == "hello"
    assert payload["duration_ms"] == 3
    assert payload["extra"] == {"user_id": 7}
    assert len([item for item in handler.filters if isinstance(item, TracePulseContextFilter)]) == 1


def test_unknown_level_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown log level"):
        configure(level="NOPE", emit=False)
