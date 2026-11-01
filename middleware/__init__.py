"""Framework adapters. The application keeps its own server."""

from __future__ import annotations

from typing import Any

from traceplus.middleware.django import TracePulseMiddleware as TracePulseDjangoMiddleware
from traceplus.middleware.fastapi import TracePulseMiddleware
from traceplus.middleware.flask import TracePulseMiddleware as TracePulseWSGIMiddleware

__all__ = [
    "TracePulseDjangoMiddleware",
    "TracePulseMiddleware",
    "TracePulseWSGIMiddleware",
    "instrument",
]


def instrument(app: Any, **kwargs: Any) -> Any:
    """Attach TracePulse to a FastAPI, Starlette, Quart, or Flask app.

    Django applications should add
    ``traceplus.middleware.django.TracePulseMiddleware`` to ``MIDDLEWARE``
    instead of calling this function.
    """
    add_middleware = getattr(app, "add_middleware", None)
    if callable(add_middleware):
        add_middleware(TracePulseMiddleware, **kwargs)
        return app
    if hasattr(app, "asgi_app"):
        app.asgi_app = TracePulseMiddleware(app.asgi_app, **kwargs)
        return app
    if hasattr(app, "wsgi_app"):
        app.wsgi_app = TracePulseWSGIMiddleware(app.wsgi_app, **kwargs)
        return app
    raise TypeError(
        "Could not detect the application. "
        "FastAPI or Starlette: app.add_middleware(TracePulseMiddleware) or instrument(app). "
        "Flask or WSGI: instrument(app) or wrap app.wsgi_app with TracePulseWSGIMiddleware. "
        "A raw ASGI app can be wrapped with TracePulseMiddleware(app). "
        "Django: add 'traceplus.middleware.django.TracePulseMiddleware' to MIDDLEWARE."
    )
