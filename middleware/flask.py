"""WSGI middleware for Flask and any other WSGI application."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from traceplus.core import begin_trace, reset_trace_context, trace_response_headers
from traceplus.middleware._common import ClosingIterable, apply_wsgi_headers, request_options, run_once

Environ = Mapping[str, Any]
StartResponse = Callable[..., Any]
WSGIApp = Callable[[Environ, StartResponse], Iterable[bytes]]


def _wsgi_headers(environ: Environ) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in environ.items():
        if not isinstance(key, str) or not key.startswith("HTTP_") or value is None:
            continue
        name = key[5:].replace("_", "-").lower()
        if name in headers:
            continue
        headers[name] = str(value)
    return headers


class TracePulseMiddleware:
    """Bind the inbound trace for the WSGI call and echo it on the response.

    The context stays active while the response body is iterated, then resets.
    """

    def __init__(
        self,
        app: WSGIApp,
        header_name: str | None = None,
        include_traceparent: bool | None = None,
    ) -> None:
        self.app = app
        self._header_name = header_name
        self._include_traceparent = include_traceparent

    def __call__(self, environ: Environ, start_response: StartResponse) -> ClosingIterable:
        header_name, include_traceparent = request_options(self._header_name, self._include_traceparent)
        tokens, trace_id, span_id = begin_trace(_wsgi_headers(environ), header_name)
        updates = trace_response_headers(trace_id, span_id, header_name, include_traceparent)

        def start_response_wrapper(status: str, response_headers: list[tuple[str, str]], exc_info: Any = None) -> Any:
            headers = apply_wsgi_headers(list(response_headers), updates)
            if exc_info is not None:
                return start_response(status, headers, exc_info)
            return start_response(status, headers)

        try:
            result = self.app(environ, start_response_wrapper)
        except Exception:
            reset_trace_context(tokens)
            raise
        if result is None:
            reset_trace_context(tokens)
            return ClosingIterable((), run_once(lambda: None))
        return ClosingIterable(result, run_once(lambda: reset_trace_context(tokens)))


def install(app: Any, **kwargs: Any) -> Any:
    """Wrap ``app.wsgi_app``. Call this after other WSGI wrappers."""
    app.wsgi_app = TracePulseMiddleware(app.wsgi_app, **kwargs)
    return app
