"""
Модуль обратной совместимости консольного логирования для simulations.
Делегирует вызовы в стандартизированный общий пакет src.common.logger.
"""

try:
    from src.common.logger import (
        log_error,
        log_info,
        log_success,
        log_warning,
    )
except ImportError:
    from common.logger import (
        log_error,
        log_info,
        log_success,
        log_warning,
    )

__all__ = [
    "log_error",
    "log_info",
    "log_success",
    "log_warning",
]
