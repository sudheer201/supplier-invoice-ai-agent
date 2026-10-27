"""Structured JSON logging with a per-invoice trace id, plus a timer helper.
Logs go to stdout so CloudWatch Logs (ECS / App Runner / Lambda) picks them up as-is."""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from contextlib import contextmanager

from app.config import settings

trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": trace_id_var.get(),
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(JsonFormatter())
        logger.addHandler(h)
        logger.setLevel(settings.log_level)
        logger.propagate = False
    return logger


def log_event(logger: logging.Logger, msg: str, **fields) -> None:
    logger.info(msg, extra={"extra_fields": fields})


@contextmanager
def timed():
    """with timed() as t: ...; t["ms"] -> elapsed milliseconds"""
    t = {"ms": 0.0}
    start = time.perf_counter()
    try:
        yield t
    finally:
        t["ms"] = round((time.perf_counter() - start) * 1000, 2)
