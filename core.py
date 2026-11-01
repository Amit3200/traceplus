"""Trace context and the JSON log contract.

The process that handles the request writes one JSON object per line through
Python's standard logging. Nothing in this module opens a socket.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, NamedTuple

DEFAULT_HEADER = "X-Request-ID"
_MAX_ID_LENGTH = 256

# Stamped onto a LogRecord at emit time so a queue listener in another thread
# still formats the trace that was active in the caller.
TP_EXTRA = "_tp_extra"
TP_BOUND = "_tp_bound"
TP_OVERRIDES = "_tp_overrides"
TP_STAMPED = "_tp_stamped"
TP_SERVICE = "_tp_service"
TP_ENVIRONMENT = "_tp_environment"
TP_CALLER_MODULE = "_tp_caller_module"

_SKIP_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "trace_id",
    "span_id",
    "parent_span_id",
    "duration_ms",
}


class TraceTokens(NamedTuple):
    """Tokens returned by :func:`set_trace_context` and consumed by :func:`reset_trace_context`."""

    trace: contextvars.Token[str]
    span: contextvars.Token[str]
    parent: contextvars.Token[str]


class _Config:
    def __init__(self) -> None:
        self.service: str | None = None
        self.environment: str | None = None
        self.header_name: str | None = None
        self.include_traceparent: bool = True

    def service_value(self) -> str:
        if self.service is not None:
            return self.service
        return (
            os.getenv("TRACEPULSE_SERVICE")
            or os.getenv("SERVICE_NAME")
            or os.getenv("K_SERVICE")
            or os.getenv("GAE_SERVICE")
            or os.getenv("AWS_LAMBDA_FUNCTION_NAME")
            or ""
        )

    def environment_value(self) -> str:
        if self.environment is not None:
            return self.environment
        return (
            os.getenv("TRACEPULSE_ENVIRONMENT")
            or os.getenv("APP_ENV")
            or os.getenv("ENVIRONMENT")
            or "development"
        )

    def header_name_value(self) -> str:
        if self.header_name is not None:
            return self.header_name
        return os.getenv("TRACEPULSE_HEADER") or DEFAULT_HEADER


config = _Config()

_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("traceplus_trace_id", default="")
_span_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("traceplus_span_id", default="")
_parent_span_var: contextvars.ContextVar[str] = contextvars.ContextVar("traceplus_parent_span_id", default="")
_bound_var: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "traceplus_bound",
    default=None,
)


def new_trace_id() -> str:
    """Return a new dashed UUID trace id."""
    return str(uuid.uuid4())


def new_span_id() -> str:
    """Return a new 16-hex-character span id."""
    return uuid.uuid4().hex[:16]


def _clean_id(value: str) -> str:
    text = str(value).replace("\x00", "").replace("\r", "").replace("\n", "").strip()
    if len(text) > _MAX_ID_LENGTH:
        return text[:_MAX_ID_LENGTH]
    return text


def set_trace_context(trace_id: str, span_id: str = "", parent_span_id: str = "") -> TraceTokens:
    """Bind trace identifiers for the current task or thread.

    The returned tokens restore the previous context when passed to
    :func:`reset_trace_context`. Incoming ids are stripped of newlines and
    capped so a request header cannot break a log line.
    """
    return TraceTokens(
        _trace_id_var.set(_clean_id(trace_id)),
        _span_id_var.set(_clean_id(span_id)),
        _parent_span_var.set(_clean_id(parent_span_id)),
    )


def reset_trace_context(tokens: TraceTokens) -> None:
    """Restore the context saved by :func:`set_trace_context`."""
    _parent_span_var.reset(tokens.parent)
    _span_id_var.reset(tokens.span)
    _trace_id_var.reset(tokens.trace)


def clear_context() -> None:
    """Drop the current trace and bound fields. Safe to call more than once."""
    _trace_id_var.set("")
    _span_id_var.set("")
    _parent_span_var.set("")
    _bound_var.set(None)


def reset_config() -> None:
    """Return process settings to their unset defaults. Used by tests."""
    config.service = None
    config.environment = None
    config.header_name = None
    config.include_traceparent = True


def get_trace_id() -> str:
    return _trace_id_var.get()


def get_span_id() -> str:
    return _span_id_var.get()


def get_parent_span_id() -> str:
    return _parent_span_var.get()


def get_bound() -> dict[str, Any]:
    current = _bound_var.get()
    return dict(current) if current else {}


@contextmanager
def trace(trace_id: str | None = None, span_id: str | None = None) -> Iterator[str]:
    """Start a root trace for a worker, cron job, or Lambda invocation.

    Nested operations inside the block should use :func:`span` or ``@tracked``.
    """
    tokens = set_trace_context(trace_id or new_trace_id(), span_id or new_span_id())
    try:
        yield get_trace_id()
    finally:
        reset_trace_context(tokens)


@contextmanager
def span(span_id: str | None = None) -> Iterator[str]:
    """Open a child span. Creates a trace when the caller does not have one."""
    if not get_trace_id():
        tokens = set_trace_context(new_trace_id(), span_id or new_span_id())
        try:
            yield get_span_id()
        finally:
            reset_trace_context(tokens)
        return

    parent_token = _parent_span_var.set(get_span_id())
    span_token = _span_id_var.set(span_id or new_span_id())
    try:
        yield get_span_id()
    finally:
        _span_id_var.reset(span_token)
        _parent_span_var.reset(parent_token)


@contextmanager
def bind(**fields: Any) -> Iterator[None]:
    """Attach fields to every log emitted inside the block."""
    if not fields:
        yield
        return
    merged = get_bound()
    merged.update(fields)
    token = _bound_var.set(merged)
    try:
        yield
    finally:
        _bound_var.reset(token)


def _is_hex(value: str, size: int) -> bool:
    if len(value) != size:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def parse_traceparent(value: str) -> tuple[str, str] | None:
    """Parse a W3C ``traceparent`` header into ``(trace_id, span_id)``."""
    parts = value.strip().split("-")
    if len(parts) < 4:
        return None
    version, trace_id, span_id = parts[0].lower(), parts[1].lower(), parts[2].lower()
    if version == "ff" or not _is_hex(trace_id, 32) or not _is_hex(span_id, 16):
        return None
    if trace_id == "0" * 32 or span_id == "0" * 16:
        return None
    return trace_id, span_id


def parse_xray(value: str) -> tuple[str, str]:
    """Parse an AWS ``X-Amzn-Trace-Id`` header into ``(root, parent)``."""
    parts: dict[str, str] = {}
    for piece in value.split(";"):
        if "=" not in piece:
            continue
        key, val = piece.split("=", 1)
        parts[key.strip()] = val.strip()
    return parts.get("Root", ""), parts.get("Parent", "")


def format_traceparent(trace_id: str, span_id: str) -> str | None:
    """Build a W3C ``traceparent`` value when both ids are valid hex."""
    trace_hex = trace_id.replace("-", "").lower()
    span_hex = span_id.replace("-", "").lower()
    if not _is_hex(trace_hex, 32) or not _is_hex(span_hex, 16):
        return None
    if trace_hex == "0" * 32 or span_hex == "0" * 16:
        return None
    return f"00-{trace_hex}-{span_hex}-01"


def _same_trace(left: str, right: str) -> bool:
    return left.replace("-", "").lower() == right.replace("-", "").lower()


def _normalize_headers(headers: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in headers.items():
        if value is None:
            continue
        name = str(key).lower()
        text = str(value).strip()
        if not text or name in normalized:
            continue
        normalized[name] = text
    return normalized


def resolve_incoming(headers: Mapping[str, str], header_name: str | None = None) -> tuple[str, str]:
    """Choose ``(trace_id, parent_span_id)`` from inbound headers.

    The configured request header wins when it is present. When that same id
    is also carried in ``traceparent`` or ``X-Amzn-Trace-Id``, the parent span
    from the standard header is kept so the next service stays on the same trace.
    """
    normalized = _normalize_headers(headers)
    preferred_name = (header_name or config.header_name_value()).lower()
    preferred = normalized.get(preferred_name, "")
    parsed_tp = parse_traceparent(normalized["traceparent"]) if normalized.get("traceparent") else None
    xray = parse_xray(normalized["x-amzn-trace-id"]) if normalized.get("x-amzn-trace-id") else ("", "")

    if preferred:
        parent = ""
        if parsed_tp and _same_trace(preferred, parsed_tp[0]):
            parent = parsed_tp[1]
        elif xray[0] and preferred == xray[0]:
            parent = xray[1]
        return _clean_id(preferred), _clean_id(parent)
    if parsed_tp:
        return _clean_id(parsed_tp[0]), _clean_id(parsed_tp[1])
    if xray[0]:
        return _clean_id(xray[0]), _clean_id(xray[1])
    for name in ("x-trace-id", "x-request-id", "x-correlation-id", "request-id"):
        if name == preferred_name:
            continue
        if normalized.get(name):
            return _clean_id(normalized[name]), ""
    return new_trace_id(), ""


def begin_trace(
    headers: Mapping[str, str] | None = None,
    header_name: str | None = None,
) -> tuple[TraceTokens, str, str]:
    """Start a request span from headers and return ``(tokens, trace_id, span_id)``."""
    trace_id, parent = resolve_incoming(headers or {}, header_name)
    tokens = set_trace_context(trace_id, new_span_id(), parent)
    return tokens, get_trace_id(), get_span_id()


@contextmanager
def incoming_trace(
    headers: Mapping[str, str] | None = None,
    header_name: str | None = None,
) -> Iterator[str]:
    """Activate a trace from inbound headers for any framework without a built-in adapter."""
    tokens, trace_id, _span_id = begin_trace(headers, header_name)
    try:
        yield trace_id
    finally:
        reset_trace_context(tokens)


def _has_header(headers: Mapping[str, str], name: str) -> bool:
    target = name.lower()
    return any(str(key).lower() == target for key in headers)


def _set_if_missing(headers: dict[str, str], name: str, value: str) -> None:
    if value and not _has_header(headers, name):
        headers[name] = value


def inject_headers(
    headers: Mapping[str, str] | None = None,
    *,
    header_name: str | None = None,
) -> dict[str, str]:
    """Return ``headers`` plus the current trace, for an outbound HTTP call."""
    merged = {str(key): str(value) for key, value in dict(headers or {}).items()}
    trace_id = get_trace_id()
    span_id = get_span_id()
    name = header_name or config.header_name_value()
    if trace_id:
        _set_if_missing(merged, name, trace_id)
    traceparent = format_traceparent(trace_id, span_id) if trace_id and span_id else None
    if traceparent:
        _set_if_missing(merged, "traceparent", traceparent)
    elif span_id:
        _set_if_missing(merged, "X-Span-ID", span_id)
    return merged


def _header_text(value: str) -> str:
    return value.encode("latin-1", errors="replace").decode("latin-1")


def trace_response_headers(
    trace_id: str,
    span_id: str,
    header_name: str | None = None,
    include_traceparent: bool | None = None,
) -> list[tuple[str, str]]:
    """Headers a server should echo so the caller can follow the same trace."""
    name = header_name or config.header_name_value()
    include = config.include_traceparent if include_traceparent is None else include_traceparent
    headers = [(name, _header_text(trace_id))]
    if include and name.lower() != "traceparent":
        traceparent = format_traceparent(trace_id, span_id)
        if traceparent:
            headers.append(("traceparent", traceparent))
    return headers


class TracePulseContextFilter(logging.Filter):
    """Copy the active trace onto the record before it leaves the emitting thread."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id()
        record.span_id = get_span_id()
        record.parent_span_id = get_parent_span_id()
        setattr(record, TP_BOUND, get_bound())
        setattr(record, TP_SERVICE, config.service_value())
        setattr(record, TP_ENVIRONMENT, config.environment_value())
        setattr(record, TP_STAMPED, True)
        return True


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _dumps(payload: dict[str, Any]) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, default=_json_default, separators=(",", ":"))
    except (TypeError, ValueError):
        safe = dict(payload)
        if "extra" in safe:
            safe["extra"] = str(safe["extra"])
        return json.dumps(safe, ensure_ascii=False, default=_json_default, separators=(",", ":"))


def _timestamp(created: float) -> str:
    return (
        datetime.fromtimestamp(created, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _collect_extra(record: logging.LogRecord, bound: dict[str, Any]) -> dict[str, Any]:
    extra = dict(bound)
    for key, value in record.__dict__.items():
        if key in _SKIP_RECORD_FIELDS or key.startswith("_"):
            continue
        extra[key] = value
    stamped = getattr(record, TP_EXTRA, None)
    if isinstance(stamped, dict):
        extra.update(stamped)
    extra.pop("duration_ms", None)
    return extra


class TracePulseJSONFormatter(logging.Formatter):
    """Format a log record as one flat JSON object."""

    def __init__(self, fmt: str | None = None, datefmt: str | None = None, style: str = "%", **kwargs: Any) -> None:
        try:
            super().__init__(fmt=fmt, datefmt=datefmt, style=style, **kwargs)
        except TypeError:
            super().__init__(fmt=fmt, datefmt=datefmt, style=style)

    def format(self, record: logging.LogRecord) -> str:
        stamped = bool(getattr(record, TP_STAMPED, False))
        if stamped:
            trace_id = getattr(record, "trace_id", "") or ""
            span_id = getattr(record, "span_id", "") or ""
            parent = getattr(record, "parent_span_id", "") or ""
            bound = dict(getattr(record, TP_BOUND, {}) or {})
            service = getattr(record, TP_SERVICE, "") or ""
            environment = getattr(record, TP_ENVIRONMENT, "") or config.environment_value()
        else:
            trace_id = get_trace_id()
            span_id = get_span_id()
            parent = get_parent_span_id()
            bound = get_bound()
            service = config.service_value()
            environment = config.environment_value()

        overrides = getattr(record, TP_OVERRIDES, None) or {}
        if not isinstance(overrides, dict):
            overrides = {}
        extra = _collect_extra(record, bound)
        duration = getattr(record, "duration_ms", None)
        if duration is None and "duration_ms" in extra:
            duration = extra.pop("duration_ms")

        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)

        payload: dict[str, Any] = {
            "timestamp": _timestamp(record.created),
            "level": record.levelname,
            "message": message,
            "trace_id": trace_id,
            "span_id": span_id,
        }
        if parent:
            payload["parent_span_id"] = parent
        if duration is not None:
            payload["duration_ms"] = duration
        payload["logger"] = record.name
        payload["module"] = overrides.get("module") or getattr(record, TP_CALLER_MODULE, None) or record.module
        payload["function"] = overrides.get("function", record.funcName)
        payload["line"] = overrides.get("line", record.lineno)
        if service:
            payload["service"] = service
        payload["environment"] = environment
        if extra:
            payload["extra"] = extra
        if record.exc_info:
            payload["exception"] = record.exc_text or self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = record.stack_info
        return _dumps(payload)
