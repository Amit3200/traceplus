"""Function timing that writes a JSON log and re-raises the original exception."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, TypeVar, overload

from traceplus.core import span
from traceplus.logger import get_logger

F = TypeVar("F", bound=Callable[..., Any])


def _level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    import logging

    value = getattr(logging, str(level).upper(), None)
    if not isinstance(value, int):
        raise ValueError(f"Unknown log level: {level}")
    return value


def _fields(func: Callable[..., Any], duration_ms: float | None = None) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "function": getattr(func, "__qualname__", getattr(func, "__name__", "unknown")),
        "module": getattr(func, "__module__", ""),
    }
    code = getattr(func, "__code__", None)
    if code is not None:
        fields["line"] = code.co_firstlineno
    if duration_ms is not None:
        fields["duration_ms"] = duration_ms
    return fields


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


def _attach(func: F, *, level: str | int, log_entry: bool) -> F:
    from functools import wraps

    logger = get_logger(getattr(func, "__module__", None) or "traceplus")
    severity = _level(level)
    func_name = getattr(func, "__qualname__", getattr(func, "__name__", "unknown"))

    if asyncio.iscoroutinefunction(func):

        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with span():
                start = time.perf_counter()
                if log_entry:
                    logger.log(severity, "Entering %s", func_name, extra=_fields(func))
                try:
                    result = await func(*args, **kwargs)
                except Exception as exc:
                    logger.error(
                        "Failed %s: %s",
                        func_name,
                        exc,
                        extra=_fields(func, _elapsed_ms(start)),
                        exc_info=True,
                    )
                    raise
                logger.log(severity, "Executed %s", func_name, extra=_fields(func, _elapsed_ms(start)))
                return result

        return async_wrapper  # type: ignore[return-value]

    @wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        with span():
            start = time.perf_counter()
            if log_entry:
                logger.log(severity, "Entering %s", func_name, extra=_fields(func))
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                logger.error(
                    "Failed %s: %s",
                    func_name,
                    exc,
                    extra=_fields(func, _elapsed_ms(start)),
                    exc_info=True,
                )
                raise
            logger.log(severity, "Executed %s", func_name, extra=_fields(func, _elapsed_ms(start)))
            return result

    return sync_wrapper  # type: ignore[return-value]


@overload
def tracked(func: F) -> F: ...


@overload
def tracked(*, level: str | int = "INFO", log_entry: bool = False) -> Callable[[F], F]: ...


def tracked(
    func: F | None = None,
    *,
    level: str | int = "INFO",
    log_entry: bool = False,
) -> Any:
    """Log the duration of ``func`` and re-raise exceptions.

    ``@tracked`` and ``@tracked(level="DEBUG", log_entry=True)`` both work.
    Place it inside ``@staticmethod`` or ``@classmethod``, or outside them.
    Arguments are not logged.
    """

    def decorator(fn: Any) -> Any:
        if isinstance(fn, staticmethod):
            return staticmethod(decorator(fn.__func__))
        if isinstance(fn, classmethod):
            return classmethod(decorator(fn.__func__))
        if not callable(fn):
            raise TypeError("@tracked requires a function")
        return _attach(fn, level=level, log_entry=log_entry)

    if func is not None:
        return decorator(func)
    return decorator
