"""Structured logging setup.

Console renderer for local development, JSON for anything that ships logs
somewhere. Timestamps are UTC ISO-8601 so log lines correlate with candle
timestamps without mental timezone arithmetic.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_configured = False


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    """Configure structlog and route stdlib logging through it.

    Idempotent: safe to call from both the app factory and test fixtures.
    """
    global _configured  # noqa: PLW0603 -- module-level init guard
    if _configured:
        return

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        # A stdlib-backed factory, not PrintLoggerFactory: `add_logger_name` reads
        # `logger.name`, which only a stdlib logger has. It also means structlog
        # output and any third-party stdlib logging share one set of handlers.
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=logging.getLevelNamesMapping()[level],
        force=True,
    )
    # uvicorn installs its own handlers; let them propagate to the root logger.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(noisy).handlers.clear()
        logging.getLogger(noisy).propagate = True

    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger. Use ``get_logger(__name__)``."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
