"""Django middleware. Add it as the first entry in ``MIDDLEWARE``.

The class supports Django's sync and async modes without importing Django.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from traceplus.core import begin_trace, reset_trace_context, trace_response_headers
from traceplus.middleware._common import (
    ClosingIterable,
    apply_mapping_headers,
    request_options,
    run_once,
)


def _django_headers(request: Any) -> dict[str, str]:
    headers = getattr(request, "headers", None)
    if headers is not None:
        return {str(key).lower(): str(value) for key, value in headers.items() if value}
    meta = getattr(request, "META", None) or {}
    extracted: dict[str, str] = {}
    for key, value in meta.items():
        if not isinstance(key, str) or not key.startswith("HTTP_") or value is None:
            continue
        name = key[5:].replace("_", "-").lower()
        if name in extracted:
            continue
        extracted[name] = str(value)
    return extracted


class TracePulseMiddleware:
    """Bind the request trace around Django's view and later middleware."""

    sync_capable = True
    async_capable = True

    def __init__(
        self,
        get_response: Callable[..., Any] | None,
        header_name: str | None = None,
        include_traceparent: bool | None = None,
    ) -> None:
        if get_response is None:
            raise ValueError("get_response must be provided.")
        self.get_response = get_response
        self._header_name = header_name
        self._include_traceparent = include_traceparent
        self._async = asyncio.iscoroutinefunction(get_response)
        if self._async:
            self._is_coroutine_marker = True
            coroutine_marker = getattr(getattr(asyncio, "coroutines", None), "_is_coroutine", True)
            self._is_coroutine = coroutine_marker

    def __call__(self, request: Any) -> Any:
        if self._async:
            return self._async_call(request)
        return self._sync_call(request)

    def _sync_call(self, request: Any) -> Any:
        tokens, trace_id, span_id = self._begin(request)
        try:
            response = self.get_response(request)
        except Exception:
            reset_trace_context(tokens)
            raise
        return self._finish(response, tokens, trace_id, span_id)

    async def _async_call(self, request: Any) -> Any:
        tokens, trace_id, span_id = self._begin(request)
        try:
            response = await self.get_response(request)
        except Exception:
            reset_trace_context(tokens)
            raise
        return self._finish(response, tokens, trace_id, span_id)

    def _begin(self, request: Any) -> tuple[Any, str, str]:
        header_name, _include = request_options(self._header_name, self._include_traceparent)
        return begin_trace(_django_headers(request), header_name)

    def _finish(self, response: Any, tokens: Any, trace_id: str, span_id: str) -> Any:
        header_name, include_traceparent = request_options(self._header_name, self._include_traceparent)
        try:
            apply_mapping_headers(
                response,
                trace_response_headers(trace_id, span_id, header_name, include_traceparent),
            )
        except Exception:
            reset_trace_context(tokens)
            raise
        if getattr(response, "streaming", False):
            self._defer_reset(response, tokens)
            return response
        reset_trace_context(tokens)
        return response

    def _defer_reset(self, response: Any, tokens: Any) -> None:
        on_close = run_once(lambda: reset_trace_context(tokens))
        content = getattr(response, "streaming_content", None)
        if content is not None:
            response.streaming_content = ClosingIterable(content, on_close)
        original_close = getattr(response, "close", None)
        if not callable(original_close):
            return

        def close() -> Any:
            try:
                return original_close()
            finally:
                on_close()

        response.close = close
