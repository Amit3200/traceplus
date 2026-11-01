"""ASGI, WSGI, and Django adapter tests. No web framework is imported."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from traceplus import get_logger, get_parent_span_id, get_span_id, get_trace_id, inject_headers, instrument, trace
from traceplus.middleware.django import TracePulseMiddleware as DjangoMiddleware
from traceplus.middleware.fastapi import TracePulseMiddleware
from traceplus.middleware.flask import TracePulseMiddleware as WSGIMiddleware


async def _call_asgi(app, headers=None, scope_type: str = "http"):
    sent: list[dict] = []
    scope = {
        "type": scope_type,
        "method": "GET",
        "path": "/users",
        "headers": [
            (str(key).lower().encode("latin-1"), str(value).encode("latin-1"))
            for key, value in dict(headers or {}).items()
        ],
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _response_headers(sent: list[dict]) -> dict[str, str]:
    start = next(message for message in sent if message["type"] == "http.response.start")
    return {key.decode("latin-1"): value.decode("latin-1") for key, value in start["headers"]}


async def _ok_app(scope, receive, send, seen: dict) -> None:
    seen["trace"] = get_trace_id()
    seen["parent"] = get_parent_span_id()
    seen["span"] = get_span_id()
    get_logger("app.web").info("hit")
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def test_asgi_echoes_request_id_and_resets(logs) -> None:
    seen: dict[str, str] = {}

    async def app(scope, receive, send):
        await _ok_app(scope, receive, send, seen)

    sent = asyncio.run(_call_asgi(TracePulseMiddleware(app), {"X-Request-ID": "req-9"}))
    headers = _response_headers(sent)
    assert seen["trace"] == "req-9"
    assert logs.records()[-1]["trace_id"] == "req-9"
    assert logs.records()[-1]["message"] == "hit"
    assert headers["x-request-id"] == "req-9"
    assert "traceparent" not in headers
    assert get_trace_id() == ""


def test_asgi_traceparent_round_trip(logs) -> None:
    with trace() as trace_id:
        client_span = get_span_id()
        outbound = inject_headers()

    seen: dict[str, str] = {}

    async def app(scope, receive, send):
        await _ok_app(scope, receive, send, seen)

    sent = asyncio.run(_call_asgi(TracePulseMiddleware(app), outbound))
    headers = _response_headers(sent)
    assert seen["trace"] == trace_id
    assert seen["parent"] == client_span
    assert seen["span"] != client_span
    assert headers["x-request-id"] == trace_id
    assert headers["traceparent"].split("-")[1] == trace_id.replace("-", "")
    assert headers["traceparent"].split("-")[2] == seen["span"]
    assert logs.records()[-1]["parent_span_id"] == client_span
    assert get_trace_id() == ""


def test_asgi_generates_a_trace_and_ignores_non_http() -> None:
    seen: dict[str, str] = {}

    async def app(scope, receive, send):
        if scope["type"] != "http":
            seen["lifespan"] = "yes"
            return
        await _ok_app(scope, receive, send, seen)

    middleware = TracePulseMiddleware(app)
    sent = asyncio.run(_call_asgi(middleware))
    uuid.UUID(seen["trace"])
    assert _response_headers(sent)["x-request-id"] == seen["trace"]
    asyncio.run(_call_asgi(middleware, scope_type="lifespan"))
    assert seen["lifespan"] == "yes"


def test_asgi_strips_header_injection(logs) -> None:
    seen: dict[str, str] = {}

    async def app(scope, receive, send):
        await _ok_app(scope, receive, send, seen)

    hostile = "abc\nFORGED\r" + ("x" * 300)
    sent = asyncio.run(_call_asgi(TracePulseMiddleware(app), {"X-Request-ID": hostile}))
    echoed = _response_headers(sent)["x-request-id"]
    assert "\n" not in echoed
    assert len(echoed) == 256
    assert all("\n" not in line for line in logs.lines())
    assert logs.records()[-1]["trace_id"] == echoed


def test_asgi_resets_when_the_app_raises() -> None:
    async def app(scope, receive, send):
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        asyncio.run(_call_asgi(TracePulseMiddleware(app), {"X-Request-ID": "req-err"}))
    assert get_trace_id() == ""


def _call_wsgi(app, headers=None):
    environ = {"REQUEST_METHOD": "GET", "PATH_INFO": "/users"}
    for key, value in dict(headers or {}).items():
        environ["HTTP_" + str(key).upper().replace("-", "_")] = str(value)
    captured: dict[str, object] = {}

    def start_response(status, response_headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = response_headers
        return lambda chunk: None

    body = app(environ, start_response)
    payload = b"".join(body)
    close = getattr(body, "close", None)
    if close:
        close()
    return captured, payload


def test_wsgi_echoes_request_id_and_resets_after_the_body(logs) -> None:
    seen: dict[str, str] = {}

    def app(environ, start_response):
        seen["trace"] = get_trace_id()
        get_logger("flask.app").info("hit")
        start_response("200 OK", [("Content-Type", "text/plain"), ("X-Request-ID", "wrong")])
        return [b"ok"]

    captured, body = _call_wsgi(WSGIMiddleware(app), {"X-Request-ID": "req-wsgi"})
    headers = {key.lower(): value for key, value in captured["headers"]}
    assert body == b"ok"
    assert seen["trace"] == "req-wsgi"
    assert headers["x-request-id"] == "req-wsgi"
    assert logs.records()[-1]["trace_id"] == "req-wsgi"
    assert get_trace_id() == ""


def test_wsgi_streaming_keeps_the_trace_until_the_body_ends() -> None:
    seen: dict[str, str] = {}

    def app(environ, start_response):
        start_response("200 OK", [])

        def gen():
            seen["during"] = get_trace_id()
            yield b"a"
            seen["still"] = get_trace_id()
            yield b"b"

        return gen()

    _captured, body = _call_wsgi(WSGIMiddleware(app), {"X-Request-ID": "stream-1"})
    assert body == b"ab"
    assert seen["during"] == "stream-1"
    assert seen["still"] == "stream-1"
    assert get_trace_id() == ""


def test_wsgi_resets_when_the_app_raises() -> None:
    def app(environ, start_response):
        raise RuntimeError("wsgi")

    with pytest.raises(RuntimeError, match="wsgi"):
        _call_wsgi(WSGIMiddleware(app), {"X-Request-ID": "req-wsgi-err"})
    assert get_trace_id() == ""


class _DjangoRequest:
    def __init__(self, headers=None, meta=None):
        self.headers = headers
        self.META = meta or {}


class _DjangoResponse:
    def __init__(self):
        self.headers: dict[str, str] = {}

    def __setitem__(self, key, value):
        self.headers[key] = value


def test_django_sync_middleware() -> None:
    seen: dict[str, str] = {}

    def get_response(request):
        seen["trace"] = get_trace_id()
        return _DjangoResponse()

    middleware = DjangoMiddleware(get_response)
    assert middleware._async is False
    response = middleware(_DjangoRequest({"X-Request-ID": "django-1"}))
    assert not asyncio.iscoroutine(response)
    assert seen["trace"] == "django-1"
    assert response.headers["X-Request-ID"] == "django-1"
    assert get_trace_id() == ""


def test_django_reads_meta_when_headers_are_missing() -> None:
    seen: dict[str, str] = {}

    def get_response(request):
        seen["trace"] = get_trace_id()
        return _DjangoResponse()

    request = _DjangoRequest(headers=None, meta={"HTTP_X_REQUEST_ID": "from-meta"})
    response = DjangoMiddleware(get_response)(request)
    assert seen["trace"] == "from-meta"
    assert response.headers["X-Request-ID"] == "from-meta"


def test_django_async_middleware() -> None:
    seen: dict[str, str] = {}

    async def get_response(request):
        seen["trace"] = get_trace_id()
        await asyncio.sleep(0)
        return _DjangoResponse()

    middleware = DjangoMiddleware(get_response)
    assert middleware._async is True
    assert middleware._is_coroutine_marker is True
    response = asyncio.run(middleware(_DjangoRequest({"X-Request-ID": "django-async"})))
    assert seen["trace"] == "django-async"
    assert response.headers["X-Request-ID"] == "django-async"
    assert get_trace_id() == ""


def test_django_streaming_response_keeps_context_while_iterating() -> None:
    class Streaming:
        def __init__(self):
            self.streaming = True
            self.headers: dict[str, str] = {}
            self.seen: list[str] = []

            def gen():
                self.seen.append(get_trace_id())
                yield b"a"
                self.seen.append(get_trace_id())
                yield b"b"

            self.streaming_content = gen()

        def __setitem__(self, key, value):
            self.headers[key] = value

        def close(self):
            closer = getattr(self.streaming_content, "close", None)
            if callable(closer):
                closer()

    response = Streaming()

    def get_response(request):
        return response

    middleware = DjangoMiddleware(get_response)
    returned = middleware(_DjangoRequest({"X-Request-ID": "dj-stream"}))
    assert b"".join(returned.streaming_content) == b"ab"
    assert response.seen == ["dj-stream", "dj-stream"]
    returned.close()
    assert get_trace_id() == ""


def test_header_name_can_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEPULSE_HEADER", "X-Correlation-ID")
    seen: dict[str, str] = {}

    async def app(scope, receive, send):
        seen["trace"] = get_trace_id()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent = asyncio.run(_call_asgi(TracePulseMiddleware(app), {"X-Correlation-ID": "corr-env"}))
    assert seen["trace"] == "corr-env"
    assert _response_headers(sent)["x-correlation-id"] == "corr-env"


def test_instrument_detects_frameworks() -> None:
    class FakeAPI:
        def __init__(self):
            self.added = []

        def add_middleware(self, cls, **kwargs):
            self.added.append((cls, kwargs))

    class FakeFlask:
        def __init__(self):
            self.wsgi_app = lambda environ, start_response: []

    class FakeQuart:
        def __init__(self):
            async def asgi(scope, receive, send):
                return None

            self.asgi_app = asgi

    api = FakeAPI()
    flask_app = FakeFlask()
    quart_app = FakeQuart()
    assert instrument(api, header_name="X-Trace-ID") is api
    assert api.added == [(TracePulseMiddleware, {"header_name": "X-Trace-ID"})]
    assert instrument(flask_app) is flask_app
    assert isinstance(flask_app.wsgi_app, WSGIMiddleware)
    assert instrument(quart_app) is quart_app
    assert isinstance(quart_app.asgi_app, TracePulseMiddleware)
    with pytest.raises(TypeError, match="Django"):
        instrument(object())
