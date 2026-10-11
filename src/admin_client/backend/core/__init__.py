"""Инициализирует базовый модуль ядра клиентского приложения."""

from .remote_client import (
    AuthenticationError,
    RemoteAdminClient,
    RemoteClientError,
    SessionExpiredError,
)

__all__ = [
    "AuthenticationError",
    "RemoteAdminClient",
    "RemoteClientError",
    "SessionExpiredError",
]
