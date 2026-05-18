"""Structured logging setup."""
import logging
import sys
from .config import get_settings


_initialized = False


def init_logging() -> None:
    global _initialized
    if _initialized:
        return
    settings = get_settings()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    # Quiet down chatty libraries
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    _initialized = True


def get_logger(name: str) -> logging.Logger:
    init_logging()
    return logging.getLogger(name)
