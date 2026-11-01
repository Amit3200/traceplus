"""Logger setup that writes TracePulse JSON through Python's standard logging."""

from __future__ import annotations

import logging
import os
import sys
import traceback
from typing import Any, TextIO

from traceplus.core import (
    TP_CALLER_MODULE,
    TP_EXTRA,
    TP_OVERRIDES,
    TracePulseContextFilter,
    TracePulseJSONFormatter,
    config,
)

_LIB_DIR = os.path.normcase(os.path.dirname(__file__))
_LIBRARY_FILES = {
    os.path.normcase(os.path.join(_LIB_DIR, name))
    for name in ("__init__.py", "core.py", "decorators.py", "logger.py")
}
_MIDDLEWARE_DIR = os.path.normcase(os.path.join(_LIB_DIR, "middleware"))
_LOGGING_SRC = os.path.normcase(getattr(logging, "_srcfile", "") or "")


def _coerce_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    value = getattr(logging, str(level).upper(), None)
    if not isinstance(value, int):
        raise ValueError(f"Unknown log level: {level}")
    return value


def _is_library_frame(frame: Any) -> bool:
    filename = os.path.normcase(frame.f_code.co_filename)
    if _LOGGING_SRC and filename == _LOGGING_SRC:
        return True
    if "importlib" in filename and "_bootstrap" in filename:
        return True
    if filename in _LIBRARY_FILES:
        return True
    return filename.startswith(_MIDDLEWARE_DIR + os.sep)


def _caller(stacklevel: int, stack_info: bool) -> tuple[str, int, str, str, str | None]:
    """Return the first frame outside TracePulse and the logging package.

    Called directly from :meth:`TracePulseAdapter.log`.
    """
    frame = sys._getframe(1).f_back
    remaining = max(int(stacklevel), 1)
    found = None
    while frame is not None:
        if _is_library_frame(frame):
            frame = frame.f_back
            continue
        remaining -= 1
        if remaining == 0:
            found = frame
            break
        frame = frame.f_back
    if found is None:
        return "(unknown file)", 0, "(unknown function)", "", None
    module_name = str(found.f_globals.get("__name__", ""))
    func_name = str(getattr(found.f_code, "co_qualname", found.f_code.co_name))
    formatted = None
    if stack_info:
        formatted = "".join(traceback.format_stack(found)).rstrip()
    return found.f_code.co_filename, found.f_lineno, func_name, module_name, formatted


class TracePulseAdapter(logging.LoggerAdapter):
    """Logger adapter that accepts any ``extra`` fields and keeps the caller's location."""

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        raw = kwargs.get("extra")
        fields: dict[str, Any] = dict(self.extra or {})
        if isinstance(raw, dict):
            fields.update(raw)
        elif raw is not None:
            fields["value"] = raw
        overrides: dict[str, Any] = {}
        for key in ("function", "module", "line"):
            if key in fields:
                overrides[key] = fields.pop(key)
        duration = fields.pop("duration_ms", None)
        stamped: dict[str, Any] = {TP_EXTRA: fields, TP_OVERRIDES: overrides}
        if duration is not None:
            stamped["duration_ms"] = duration
        kwargs["extra"] = stamped
        return msg, kwargs

    def log(self, level: int, msg: Any, *args: Any, **kwargs: Any) -> None:
        if not self.isEnabledFor(level):
            return
        msg, kwargs = self.process(msg, kwargs)
        exc_info = kwargs.pop("exc_info", None)
        extra = kwargs.pop("extra", None)
        stack_info = bool(kwargs.pop("stack_info", False))
        stacklevel = kwargs.pop("stacklevel", 1)
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            raise TypeError(f"Logger got unexpected keyword argument(s): {unexpected}")
        if exc_info:
            if isinstance(exc_info, BaseException):
                exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
            elif not isinstance(exc_info, tuple):
                exc_info = sys.exc_info()
        filename, lineno, func_name, module_name, sinfo = _caller(stacklevel, stack_info)
        record = self.logger.makeRecord(
            self.logger.name,
            level,
            filename,
            lineno,
            msg,
            args,
            exc_info,
            func_name,
            extra,
            sinfo,
        )
        setattr(record, TP_CALLER_MODULE, module_name)
        self.logger.handle(record)


def get_logger(name: str | None = None) -> TracePulseAdapter:
    """Return a logger that emits TracePulse fields from ``extra``."""
    return TracePulseAdapter(logging.getLogger(name or "traceplus"), {})


def apply_to_handler(handler: logging.Handler) -> logging.Handler:
    """Attach the TracePulse filter and JSON formatter to an existing handler."""
    if not any(isinstance(existing, TracePulseContextFilter) for existing in handler.filters):
        handler.addFilter(TracePulseContextFilter())
    handler.setFormatter(TracePulseJSONFormatter())
    return handler


def configure(
    *,
    service: str | None = None,
    environment: str | None = None,
    level: str | int | None = None,
    header_name: str | None = None,
    include_traceparent: bool | None = None,
    stream: TextIO | None = None,
    logger_name: str | None = None,
    emit: bool = True,
    force: bool = False,
) -> logging.Logger:
    """Install JSON logging, or only set service metadata when ``emit`` is false.

    Call this once at process start for a new application. An application that
    already has ``dictConfig`` or ``LOGGING`` should keep that handler and call
    :func:`apply_to_handler` instead, with ``emit=False`` here if it still wants
    to set the service name in code.
    """
    if service is not None:
        config.service = service
    if environment is not None:
        config.environment = environment
    if header_name is not None:
        config.header_name = header_name
    if include_traceparent is not None:
        config.include_traceparent = include_traceparent

    target = logging.getLogger(logger_name) if logger_name else logging.getLogger()
    if level is not None:
        target.setLevel(_coerce_level(level))
    elif emit and target.level == logging.NOTSET:
        target.setLevel(logging.INFO)

    owned = [handler for handler in target.handlers if getattr(handler, "_traceplus", False)]
    if force:
        for handler in owned:
            target.removeHandler(handler)
        owned = []
    if not emit or owned:
        return target

    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    apply_to_handler(handler)
    handler._traceplus = True  # type: ignore[attr-defined]
    target.addHandler(handler)
    if logger_name is not None:
        target.propagate = False
    return target
