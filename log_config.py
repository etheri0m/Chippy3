"""
Shared logging configuration for all ChippyPi scripts.
Import: from log_config import get_logger
"""

import sys
from loguru import logger

# Remove default handler
logger.remove()

# Console: coloured, concise format
logger.add(
    sys.stderr,
    level="DEBUG",
    format=(
        "<green>{time:HH:mm:ss.SSS}</green> | "
        "<level>{level:<7}</level> | "
        "<cyan>{extra[module]:<12}</cyan> | "
        "<level>{message}</level>"
    ),
    colorize=True,
)

# File: rotating log, keeps last 3 days
logger.add(
    "/tmp/chippy_{time:YYYY-MM-DD}.log",
    level="DEBUG",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<7} | {extra[module]:<12} | {message}",
    rotation="00:00",
    retention="3 days",
    compression="gz",
)


def get_logger(module_name: str):
    """Return a logger bound to a specific module name."""
    return logger.bind(module=module_name)