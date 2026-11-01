# TracePulse

TracePulse is a Python library for structured JSON logs and request traces. It runs inside the application process you already have and writes one JSON object per line through Python's standard `logging`. CloudWatch, Google Cloud Logging, and any agent that already ships stdout can index those lines.

There is no TracePulse server to deploy. The JSON schema is the contract, so a service in another language joins the same traces by writing the same kind of line.

Requires Python 3.9 or newer. The package has no runtime dependencies, and the framework adapters do not import FastAPI, Flask, or Django.

## Install

From PyPI:

```bash
pip install traceplus-py
```

Add the dev tools with `pip install "traceplus-py[dev]"`.

From this repository:

```bash
pip install -e ".[dev]"
```

Use `pip install -e .` when you do not need the test tools.

Service, environment, and the request header are read from the process environment when you do not pass them to `configure()`:

| Setting | Environment variables, first match wins |
| --- | --- |
| Service | `TRACEPULSE_SERVICE`, `SERVICE_NAME`, `K_SERVICE`, `GAE_SERVICE`, `AWS_LAMBDA_FUNCTION_NAME` |
| Environment | `TRACEPULSE_ENVIRONMENT`, `APP_ENV`, `ENVIRONMENT` |
| Request header | `TRACEPULSE_HEADER` (default `X-Request-ID`) |

Call `configure()` once at process start. It attaches a JSON handler to the root logger. If the process already has handlers, use the [existing logging](#applications-that-already-configure-logging) sample instead.

## Quick start

```python
from traceplus import configure, get_logger, tracked

configure(service="billing", environment="production")
logger = get_logger(__name__)

@tracked
def get_user_profile(user_id: int) -> dict:
    logger.info("User profile fetched", extra={"user_id": user_id})
    return {"id": user_id}

get_user_profile(9821)
```

That call writes two lines. The first is your log. The second is the timing line from `@tracked`. With no trace already active, `@tracked` creates one, and both lines share it. `parent_span_id` appears when middleware or `trace()` started the trace before the call. Timestamps, ids, and line numbers follow the caller.

```json
{"timestamp":"2026-11-01T12:20:58.833Z","level":"INFO","message":"User profile fetched","trace_id":"a1b2c3d4-e5f6-7890-abcd-ef0123456789","span_id":"2af743206fd8473f","logger":"services.user","module":"services.user","function":"get_user_profile","line":12,"service":"billing","environment":"production","extra":{"user_id":9821}}
{"timestamp":"2026-11-01T12:20:58.833Z","level":"INFO","message":"Executed get_user_profile","trace_id":"a1b2c3d4-e5f6-7890-abcd-ef0123456789","span_id":"2af743206fd8473f","duration_ms":0.15,"logger":"services.user","module":"services.user","function":"get_user_profile","line":10,"service":"billing","environment":"production"}
```

`duration_ms`, `function`, `module`, and `line` are top-level fields. Every other value passed in `extra` is nested under `extra`. `@tracked` logs success at `INFO`, logs failures at `ERROR` with an `exception` traceback, and re-raises the original exception. It does not log arguments. Pass `log_entry=True` to also log the call entry, or `level="DEBUG"` to quiet a noisy function:

```python
@tracked(level="DEBUG", log_entry=True)
def load_cache() -> None:
    ...
```

## Samples

### FastAPI

Add `instrument(app)` after your other `add_middleware(...)` calls. Starlette runs the last middleware you add as the outermost layer, which is what puts the trace id on every log inside the request.

Put `@tracked` below the route decorator so the wrapper is the function FastAPI calls.

```python
from fastapi import FastAPI
from traceplus import configure, get_logger, instrument, tracked

configure(service="billing", environment="production")
app = FastAPI()
instrument(app)
logger = get_logger(__name__)

@app.get("/users/{user_id}")
@tracked
async def get_user(user_id: int) -> dict:
    logger.info("User profile fetched", extra={"user_id": user_id})
    return {"id": user_id}
```

A client can send `X-Request-ID: req-9`. The response echoes that header, and every log during the request uses `req-9` as `trace_id`. With no inbound id, TracePulse generates one. When the ids are valid hex, the response also includes a W3C `traceparent`.

The same `instrument(app)` call works for Starlette, Flask, and Quart. A raw ASGI app can be wrapped directly:

```python
from traceplus.middleware.fastapi import TracePulseMiddleware

app = TracePulseMiddleware(app)
```

### Flask

```python
from flask import Flask
from traceplus import configure, get_logger, instrument, tracked

configure(service="billing", environment="production")
app = Flask(__name__)
instrument(app)
logger = get_logger(__name__)

@app.get("/users/<int:user_id>")
@tracked
def get_user(user_id: int) -> dict:
    logger.info("User profile fetched", extra={"user_id": user_id})
    return {"id": user_id}
```

Call `instrument(app)` after other code that replaces `app.wsgi_app`, so the trace context wraps those layers. A raw WSGI app can be wrapped with `TracePulseMiddleware` from `traceplus.middleware.flask`.

### Django

Put TracePulse first in `MIDDLEWARE` so it is the outermost layer. Point the existing `LOGGING` dict at the TracePulse formatter. Use `configure(emit=False)` only to set the service name in code. Calling `configure()` with the default `emit=True` adds a second handler and every line is written twice.

```python
# settings.py
from traceplus import configure

configure(service="billing", environment="production", emit=False)

MIDDLEWARE = [
    "traceplus.middleware.django.TracePulseMiddleware",
    # the rest of the middleware
]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "traceplus": {"()": "traceplus.TracePulseContextFilter"},
    },
    "formatters": {
        "traceplus": {"()": "traceplus.TracePulseJSONFormatter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["traceplus"],
            "formatter": "traceplus",
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}
```

```python
# views.py
from django.http import JsonResponse
from traceplus import get_logger, tracked

logger = get_logger(__name__)

@tracked
def user_profile(request, user_id: int):
    logger.info("User profile fetched", extra={"user_id": user_id})
    return JsonResponse({"id": user_id})
```

### Workers, cron, and Lambda

`trace()` starts a root trace for code that has no HTTP middleware. `@tracked` and `span()` open child spans inside it.

```python
from traceplus import configure, get_logger, trace, tracked

configure(service="orders")  # Lambda also fills this from AWS_LAMBDA_FUNCTION_NAME
logger = get_logger(__name__)

@tracked
def process(event: dict) -> None:
    logger.info("Order received", extra={"order_id": event["order_id"]})

def handler(event, context):
    with trace(getattr(context, "aws_request_id", None)):
        process(event)
```

Context is stored in `contextvars`, so async tasks and threads started after `trace()` inherit the same `trace_id`.

For a queue consumer or gRPC handler, activate the trace from the inbound metadata:

```python
from traceplus import incoming_trace

with incoming_trace(message.headers) as trace_id:
    handle(message)
```

Inbound ids are accepted from the configured header, W3C `traceparent`, AWS `X-Amzn-Trace-Id`, `X-Trace-ID`, `X-Request-ID`, `X-Correlation-ID`, and `Request-Id`. X-Ray's `Sampled` flag does not drop the log line.

### Applications that already configure logging

Keep the handler you already have. `emit=False` records the service name and does not add another handler.

```python
import logging
from traceplus import TracePulseContextFilter, TracePulseJSONFormatter, configure

configure(service="billing", environment="production", emit=False)

handler = logging.StreamHandler()
handler.addFilter(TracePulseContextFilter())
handler.setFormatter(TracePulseJSONFormatter())
logging.getLogger().addHandler(handler)
```

`apply_to_handler(handler)` does the filter and formatter attachment in one call. The filter copies `trace_id` onto the record in the emitting thread, so a `QueueHandler` listener in another thread still formats the right trace.

The line must be a single JSON object with no text prefix. A prefix from another formatter makes CloudWatch and Cloud Logging treat the line as plain text.

### Calling another service

```python
from traceplus import inject_headers
import urllib.request

request = urllib.request.Request(url, headers=inject_headers())
```

`requests` and `httpx` take the same dictionary:

```python
import requests
from traceplus import inject_headers

requests.get(url, headers=inject_headers({"Accept": "application/json"}))
```

Headers you already set are left in place. The next TracePulse process, or anything that understands `traceparent`, continues the same trace. When the current ids are not valid hex, TracePulse sends `X-Request-ID` and `X-Span-ID` and skips `traceparent`.

`bind()` adds fields to every log inside the block. A field passed on the log call wins over a bound field of the same name.

```python
from traceplus import bind, get_logger

logger = get_logger(__name__)

with bind(user_id=9821):
    logger.info("User profile fetched")
```

If a browser calls the API directly, expose `X-Request-ID` and `traceparent` in your CORS configuration.

## Log fields

| Field | When it is present |
| --- | --- |
| `timestamp` | Always. UTC, millisecond precision, `Z` suffix. |
| `level` | Always. `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `message` | Always. |
| `trace_id` | Always. Empty when no trace is active. |
| `span_id` | Always. Empty when no span is active. |
| `parent_span_id` | When this span has a parent. |
| `duration_ms` | On `@tracked` exit lines, and when you pass `extra={"duration_ms": ...}`. |
| `logger` | Always. The logger name. |
| `module` | Always. The Python module, such as `services.user`. |
| `function` | Always. |
| `line` | Always. |
| `service` | When a service name is configured or discovered from the environment. |
| `environment` | Always. Defaults to `development`. |
| `extra` | When the log has fields other than the ones above. |
| `exception` | When the log was emitted with exception info. Newlines are escaped so the record stays one line. |
| `stack` | When the log was emitted with `stack_info=True`. |

A failure from `@tracked` looks like this:

```json
{"timestamp":"2026-11-01T12:21:00.100Z","level":"ERROR","message":"Failed get_user_profile: profile missing","trace_id":"a1b2c3d4-e5f6-7890-abcd-ef0123456789","span_id":"2af743206fd8473f","duration_ms":1.2,"logger":"services.user","module":"services.user","function":"get_user_profile","line":18,"service":"billing","environment":"production","exception":"Traceback (most recent call last):\n  ...\nKeyError: profile missing"}
```

## Querying

AWS CloudWatch Logs Insights:

```sql
fields @timestamp, message, trace_id, duration_ms, function
| filter level = "ERROR" or duration_ms > 200
| sort @timestamp desc
| limit 50
```

One request across every service that logged it:

```sql
fields @timestamp, service, message, function, duration_ms
| filter trace_id = "a1b2c3d4-e5f6-7890-abcd-ef0123456789"
| sort @timestamp asc
```

Google Cloud Logging:

```text
jsonPayload.level="ERROR" OR jsonPayload.duration_ms > 200
```

```text
jsonPayload.trace_id="a1b2c3d4-e5f6-7890-abcd-ef0123456789"
```

## Notes

- Do not put secrets in `extra`. `@tracked` never logs function arguments.
- Inbound ids are stripped of newlines and limited to 256 characters.
- `@tracked` measures the function body. Time spent later consuming a returned generator is not included.
- Use one JSON formatter on a logger. A second formatter writes a second line.
- Place `@tracked` inside `@staticmethod` or `@classmethod`, or outside them. Both orders work.
- Open a manual child span with `with span():` when you do not want a timing log.

## Tests

```bash
pytest
```
