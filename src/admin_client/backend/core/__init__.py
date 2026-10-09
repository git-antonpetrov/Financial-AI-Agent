"""Инициализирует базовый модуль ядра клиентского приложения."""

from .remote_client import (
    RemoteAdminClient,
    RemoteClientError,
    AuthenticationError,
    SessionExpiredError,
)

__all__ = [
    "RemoteAdminClient",
    "RemoteClientError",
    "AuthenticationError",
    "SessionExpiredError",
]
