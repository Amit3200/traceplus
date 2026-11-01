"""ASGI middleware for FastAPI, Starlette, and any other ASGI application.

This is a pure ASGI callable. It does not subclass Starlette's
``BaseHTTPMiddleware``, which runs the application in a separate task and
drops ``contextvars`` before the endpoint logs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from traceplus.core import begin_trace, reset_trace_context, trace_response_headers
from traceplus.middleware._common import apply_asgi_headers, request_options

Scope = Mapping[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def _asgi_headers(scope: Scope) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in scope.get("headers") or []:
        name = key.decode("latin-1").lower()
        if name in headers:
            continue
        headers[name] = value.decode("latin-1")
    return headers


class TracePulseMiddleware:
    """Read the inbound trace id, bind it for the request, and echo it back."""

    def __init__(
        self,
        app: Callable[[Scope, Receive, Send], Awaitable[None]],
        header_name: str | None = None,
        include_traceparent: bool | None = None,
    ) -> None:
        self.app = app
        self._header_name = header_name
        self._include_traceparent = include_traceparent

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        header_name, include_traceparent = request_options(self._header_name, self._include_traceparent)
        tokens, trace_id, span_id = begin_trace(_asgi_headers(scope), header_name)
        updates = trace_response_headers(trace_id, span_id, header_name, include_traceparent)

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                raw = list(message.get("headers") or [])
                message = {**message, "headers": apply_asgi_headers(raw, updates)}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            reset_trace_context(tokens)
