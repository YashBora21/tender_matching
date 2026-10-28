"""Logging must not corrupt a command's machine-readable stdout (D7)."""

from __future__ import annotations

import logging
import sys

from app.core import logging as logging_module


def test_logs_go_to_stderr_so_json_on_stdout_stays_parseable(monkeypatch):
    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    monkeypatch.setattr(logging_module, "_CONFIGURED", False)
    try:
        logging_module.configure_logging("INFO")
        assert root.handlers
        assert root.handlers[0].stream is sys.stderr
    finally:
        root.handlers = previous_handlers
        logging_module._CONFIGURED = False
