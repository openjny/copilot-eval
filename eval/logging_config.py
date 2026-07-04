"""Structured logging configuration for the eval framework.

Diagnostic output (progress, warnings, errors) goes through the standard
``logging`` module so it can be filtered by level and, optionally, emitted as
machine-parseable JSON. User-facing output (run banners, result tables, reports)
stays on ``click.echo``. Configuration is driven by CLI flags with
``EVAL_LOG_LEVEL`` / ``EVAL_LOG_FORMAT`` environment variables as fallbacks.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

#: Logger name shared by every module in the package. Module loggers created via
#: ``logging.getLogger(__name__)`` (e.g. ``eval.runner``) propagate up to it.
ROOT_LOGGER_NAME = "eval"

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
LOG_FORMATS = ("plain", "json")

DEFAULT_LEVEL = "INFO"
DEFAULT_FORMAT = "plain"

_PLAIN_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class JsonFormatter(logging.Formatter):
    """Render each log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, _DATE_FORMAT),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _resolve_level(level: str | None) -> int:
    name = (level or os.environ.get("EVAL_LOG_LEVEL") or DEFAULT_LEVEL).upper()
    resolved = logging.getLevelName(name)
    if not isinstance(resolved, int):
        raise ValueError(f"Invalid log level: {name!r}. Choose one of {', '.join(LOG_LEVELS)}.")
    return resolved


def _resolve_format(fmt: str | None) -> str:
    name = (fmt or os.environ.get("EVAL_LOG_FORMAT") or DEFAULT_FORMAT).lower()
    if name not in LOG_FORMATS:
        raise ValueError(f"Invalid log format: {name!r}. Choose one of {', '.join(LOG_FORMATS)}.")
    return name


def configure_logging(level: str | None = None, fmt: str | None = None) -> None:
    """Configure the package logger with a single stderr handler.

    Args:
        level: Log level name (case-insensitive). Falls back to
            ``EVAL_LOG_LEVEL`` and then ``INFO``.
        fmt: Output format, ``plain`` or ``json`` (case-insensitive). Falls back
            to ``EVAL_LOG_FORMAT`` and then ``plain``.
    """
    resolved_level = _resolve_level(level)
    resolved_format = _resolve_format(fmt)

    handler = logging.StreamHandler()  # defaults to stderr
    if resolved_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_PLAIN_FORMAT, datefmt=_DATE_FORMAT))

    logger = logging.getLogger(ROOT_LOGGER_NAME)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(resolved_level)
    # Keep diagnostic output off the root logger so it never duplicates or leaks
    # into libraries that configure their own handlers.
    logger.propagate = False
