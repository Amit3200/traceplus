"""Shared logging capture for TracePulse tests."""

from __future__ import annotations

import io
import json
import logging

import pytest

from traceplus import configure
from traceplus.core import clear_context, reset_config


class CapturedLogs:
    def __init__(self, buffer: io.StringIO) -> None:
        self.buffer = buffer

    def lines(self) -> list[str]:
        return [line for line in self.buffer.getvalue().splitlines() if line.strip()]

    def records(self) -> list[dict]:
        return [json.loads(line) for line in self.lines()]


@pytest.fixture(autouse=True)
def _isolate_traceplus():
    clear_context()
    reset_config()
    yield
    clear_context()
    reset_config()


@pytest.fixture
def logs() -> CapturedLogs:
    root = logging.getLogger()
    previous_level = root.level
    previous_handlers = list(root.handlers)
    buffer = io.StringIO()
    configure(service="billing", environment="test", level="DEBUG", stream=buffer, force=True)
    captured = CapturedLogs(buffer)
    yield captured
    root.handlers = previous_handlers
    root.setLevel(previous_level)
