"""Centralized logging configuration using loguru."""

import logging
import sys
import traceback
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

from loguru import logger

trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)


NOISY_CONNECTION_LOGGERS = {
    # WebSocket accepted / HTTP access lines from uvicorn.
    "uvicorn.access": logging.WARNING,
    # "connection open" / "connection closed" emitted by websockets.
    "websockets": logging.WARNING,
    "websockets.server": logging.WARNING,
    "websockets.client": logging.WARNING,
    "uvicorn.protocols.websockets.websockets_impl": logging.WARNING,
    # Supress "Failed to parse headers" warning from urllib3 when interacting with MinIO.
    "urllib3.connection": logging.ERROR,
}


def get_trace_id() -> str | None:
    """Get current trace ID from context."""
    return trace_id_var.get()


def set_trace_id(trace_id: str) -> str:
    """Bind only an internal 12-hex trace ID and return the safe value."""
    normalized = (
        trace_id
        if len(trace_id) == 12 and all(char in "0123456789abcdef" for char in trace_id)
        else ""
    )
    safe_trace_id = normalized or uuid4().hex[:12]
    trace_id_var.set(safe_trace_id)
    return safe_trace_id


def new_trace_id() -> str:
    """Generate a new 12-char trace ID and bind it to the current context.

    Intended for background tasks that run outside HTTP/WebSocket request
    scopes so that all log lines produced by one task execution share the
    same trace_id.
    """
    return set_trace_id(uuid4().hex[:12])


def privacy_safe_shape(value: Any) -> str:
    """Describe an untrusted value without logging content or field names."""
    if value is None:
        return "none"
    if isinstance(value, str):
        return f"str_chars={len(value)}"
    if isinstance(value, bytes):
        return f"bytes={len(value)}"
    if isinstance(value, Mapping):
        return f"mapping_items={len(value)}"
    if isinstance(value, Sequence):
        return f"sequence_items={len(value)}"
    return f"type={type(value).__name__}"


def _privacy_safe_filter(record: dict[str, Any]) -> bool:
    """Remove exception values while retaining a useful, content-free trace shape."""
    record["extra"]["trace_id"] = get_trace_id() or uuid4().hex[:12]
    record["extra"]["safe_exception"] = ""

    exception = record.get("exception")
    if not exception:
        active_exception = sys.exc_info()
        if active_exception[0] is not None:
            exception = active_exception
    if not exception:
        return True

    exc_type, exc_value, tb = exception
    exc_name = getattr(exc_type, "__name__", "Exception")
    try:
        exc_text = str(exc_value)
    except Exception:
        exc_text = ""
    if exc_text:
        record["message"] = record["message"].replace(exc_text, "<redacted_exception>")

    frames = traceback.extract_tb(tb)[-8:] if tb is not None else []
    frame_shape = " <- ".join(f"{frame.name}:{frame.lineno}" for frame in frames)
    suffix = f"\nexception_type={exc_name}"
    if frame_shape:
        suffix += f" frames={frame_shape}"
    record["extra"]["safe_exception"] = suffix
    # The queue and Loguru's own sink-error reporter must never retain the raw value.
    record["exception"] = None
    return True


def _privacy_safe_format(_: dict[str, Any]) -> str:
    """Format logs without Loguru's raw exception value or local variables."""
    return (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | "
        "<cyan>{extra[trace_id]:-<12}</cyan> | <cyan>{name}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>{extra[safe_exception]}\n"
    )


def _disable_agentbay_logger_override():
    """Disable AgentBay SDK's logging override to prevent it from resetting loguru."""
    if "agentbay._common.logger" in sys.modules:
        try:
            from agentbay._common.logger import AgentBayLogger
            AgentBayLogger._initialized = True
            AgentBayLogger.setup = classmethod(lambda cls, *args, **kwargs: None)
        except Exception:
            pass


def configure_logging():
    """Configure loguru with custom format including trace ID."""
    # Remove default handler
    logger.remove()

    # Add stdout handler with custom format and filter to ensure trace_id exists
    logger.add(
        sys.stdout,
        level="INFO",
        format=_privacy_safe_format,
        enqueue=True,
        backtrace=False,
        diagnose=False,
        filter=_privacy_safe_filter,
    )

    _disable_agentbay_logger_override()

    return logger


def quiet_noisy_connection_loggers() -> None:
    """Reduce chatty transport-level logs while keeping warnings/errors visible."""
    for logger_name, level in NOISY_CONNECTION_LOGGERS.items():
        target = logging.getLogger(logger_name)
        target.setLevel(level)


class InterceptHandler(logging.Handler):
    """Forward application records and shape third-party records at the boundary."""

    def emit(self, record):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        source = str(record.name or "")
        if not source or len(source) > 128 or not all(
            char.isalnum() or char in "._-" for char in source
        ):
            source = "unknown"

        level_name = str(record.levelname).upper()
        if level_name not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            level_name = "CUSTOM"

        status_code = getattr(record, "status_code", None)
        safe_status_code: int | str = "none"
        if (
            isinstance(status_code, int)
            and not isinstance(status_code, bool)
            and 100 <= status_code <= 599
        ):
            safe_status_code = status_code

        exception_type = "none"
        if record.exc_info and record.exc_info[0] is not None:
            candidate = getattr(record.exc_info[0], "__name__", "")
            if candidate and len(candidate) <= 80 and candidate.replace("_", "").isalnum():
                exception_type = candidate

        diagnostic_shape = (
            f"source={source} level={level_name} "
            f"args_shape={privacy_safe_shape(record.args)} "
            f"status_code={safe_status_code} exception_type={exception_type}"
        )

        try:
            raw_message = record.getMessage()
        except Exception:
            message = (
                "Standard log formatting failed "
                f"message_type={type(record.msg).__name__} "
                f"{diagnostic_shape}"
            )
        else:
            message = (
                f"Standard log event {diagnostic_shape} message_chars={len(raw_message)}"
            )

        logger.opt(depth=depth, exception=record.exc_info).log(level, message)


def intercept_standard_logging():
    """Redirect standard library logging to loguru."""

    # Replace all standard logger handlers
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in logging.root.manager.loggerDict:
        logging.getLogger(name).handlers = [InterceptHandler()]
        logging.getLogger(name).propagate = False
    quiet_noisy_connection_loggers()


# Configure the imported Loguru singleton on import.
configure_logging()
