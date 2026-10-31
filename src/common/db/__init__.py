"""Общие примитивы подключения к PostgreSQL для сервисов платформы."""

from .asyncpg_ssl import build_asyncpg_ssl, database_url_without_ssl_query, libpq_sslmode

__all__ = [
    "build_asyncpg_ssl",
    "database_url_without_ssl_query",
    "libpq_sslmode",
]
