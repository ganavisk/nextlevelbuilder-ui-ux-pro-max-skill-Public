"""Structured JSON logging via structlog.

Every log line carries a `call_id` / `tenant_id` when bound in request
context, so a single call can be traced end-to-end across the webhook
handlers, tool executor, and post-call workers.
"""

import logging
from typing import cast

import structlog


def configure_logging(log_level: str = "INFO", json_output: bool = True) -> None:
    logging.basicConfig(level=log_level.upper(), format="%(message)s")

    renderer = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))
