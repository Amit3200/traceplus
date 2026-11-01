"""Shared HTTP helpers for the framework adapters."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import Any

from traceplus.core import config


def request_options(
    header_name: str | None,
    include_traceparent: bool | None,
) -> tuple[str, bool]:
    name = header_name or config.header_name_value()
    include = config.include_traceparent if include_traceparent is None else include_traceparent
    return name, include


def run_once(callback: Callable[[], None]) -> Callable[[], None]:
    state = {"done": False}

    def wrapper() -> None:
        if state["done"]:
            return
        state["done"] = True
        callback()

    return wrapper


class ClosingIterable:
    """Iterate a WSGI or Django body and run ``on_close`` exactly once."""

    def __init__(self, iterable: Iterable[Any], on_close: Callable[[], None]) -> None:
        self._iterable = iterable
        self._iterator: Iterator[Any] = iter(iterable)
        self._on_close = on_close
        self._closed = False

    def __iter__(self) -> ClosingIterable:
        return self

    def __next__(self) -> Any:
        try:
            return next(self._iterator)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            closer = getattr(self._iterable, "close", None)
            if callable(closer) and closer is not self.close:
                closer()
        finally:
            self._on_close()


def apply_wsgi_headers(
    response_headers: list[tuple[str, str]],
    updates: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    replacing = {name.lower() for name, _value in updates}
    updated = [(key, value) for key, value in response_headers if key.lower() not in replacing]
    updated.extend(updates)
    return updated


def apply_asgi_headers(raw_headers: list[tuple[bytes, bytes]], updates: list[tuple[str, str]]) -> list[tuple[bytes, bytes]]:
    replacements: dict[bytes, bytes] = {}
    for name, value in updates:
        replacements[name.lower().encode("latin-1")] = value.encode("latin-1")
    updated = [(key, value) for key, value in raw_headers if key.lower() not in replacements]
    updated.extend(replacements.items())
    return updated


def apply_mapping_headers(response: Any, updates: list[tuple[str, str]]) -> None:
    for name, value in updates:
        try:
            response[name] = value
        except Exception:
            headers = getattr(response, "headers", None)
            if headers is None:
                raise
            headers[name] = value
