"""Проверяет, что пустой POSTGRES_SSLMODE включает verify-full."""

import os

from src.common.db.asyncpg_ssl import build_asyncpg_ssl, libpq_sslmode


def test_empty_postgres_sslmode_is_verify_full():
    """Пустое значение включает проверку сертификата, disable оставляет канал без TLS."""
    ca_file = os.environ["SSL_CERT_FILE"]
    context = build_asyncpg_ssl("", ca_file=ca_file)
    assert context is not None
    assert context.check_hostname is True
    assert libpq_sslmode("") == "verify-full"
    assert libpq_sslmode(None) == "verify-full"
    assert build_asyncpg_ssl("disable") is None
    assert libpq_sslmode("disable") is None
