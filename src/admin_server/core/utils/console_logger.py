"""
Модуль обратной совместимости консольного логирования для admin_server.
Делегирует вызовы в стандартизированный общий пакет src.common.logger.
"""

try:
    from src.common.logger import (
        log_info,
        log_success,
        log_warning,
        log_error,
    )
except ImportError:
    from common.logger import (
        log_info,
        log_success,
        log_warning,
        log_error,
    )

__all__ = [
    "log_info",
    "log_success",
    "log_warning",
    "log_error",
]
