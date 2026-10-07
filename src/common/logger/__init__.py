"""
Пакет корпоративного логирования.
Экспортирует функции стандартизированного цветного логирования.
"""

from .console_logger import (
    log_error,
    log_info,
    log_success,
    log_warning,
)

__all__ = [
    "log_info",
    "log_success",
    "log_warning",
    "log_error",
]
