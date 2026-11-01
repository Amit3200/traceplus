"""Structured JSON logging and trace context for the infrastructure you already run.

TracePulse is a library. It does not open a port, ship a collector, or require
a sidecar. Each process writes one JSON object per log line to the logging
handler it already uses. CloudWatch, Cloud Logging, and any agent that tails
stdout can index those lines.
"""

from traceplus.core import (
    DEFAULT_HEADER,
    TracePulseContextFilter,
    TracePulseJSONFormatter,
    TraceTokens,
    bind,
    get_parent_span_id,
    get_span_id,
    get_trace_id,
    incoming_trace,
    inject_headers,
    new_span_id,
    new_trace_id,
    reset_trace_context,
    resolve_incoming,
    set_trace_context,
    span,
    trace,
    trace_response_headers,
)
from traceplus.decorators import tracked
from traceplus.logger import apply_to_handler, configure, get_logger
from traceplus.middleware import instrument

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_HEADER",
    "TracePulseContextFilter",
    "TracePulseJSONFormatter",
    "TraceTokens",
    "__version__",
    "apply_to_handler",
    "bind",
    "configure",
    "get_logger",
    "get_parent_span_id",
    "get_span_id",
    "get_trace_id",
    "incoming_trace",
    "inject_headers",
    "instrument",
    "new_span_id",
    "new_trace_id",
    "reset_trace_context",
    "resolve_incoming",
    "set_trace_context",
    "span",
    "trace",
    "trace_response_headers",
    "tracked",
]
