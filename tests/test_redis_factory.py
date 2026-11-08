"""Проверяет TLS-параметры фабрики клиента Redis."""

from src.common.redis_factory import redis_connection_kwargs


def test_redis_tls_checks_hostname_and_keeps_password(tmp_path):
    """Проверяет, что TLS без файла CA не собирает соединение без проверки сертификата."""
    missing = tmp_path / "missing.crt"
    try:
        redis_connection_kwargs("redis", 6379, "secret", use_ssl=True, ca_file=str(missing))
    except RuntimeError as error:
        assert "CA" in str(error)
    else:
        raise AssertionError("TLS без файла CA не должен возвращать аргументы")


def test_redis_tls_requires_certificate_when_ca_file_exists(tmp_path):
    """Проверяет, что при наличии CA клиент требует сертификат и сверяет имя."""
    ca_file = tmp_path / "ca.crt"
    ca_file.write_text("dummy", encoding="utf-8")
    kwargs = redis_connection_kwargs(
        "redis",
        6379,
        "secret",
        use_ssl=True,
        ca_file=str(ca_file),
    )
    assert kwargs["password"] == "secret"
    assert kwargs["ssl_ca_certs"] == str(ca_file)
    assert kwargs["ssl_cert_reqs"] == "required"
    assert kwargs["ssl_check_hostname"] is True


def test_redis_without_tls_does_not_check_hostname():
    """Проверяет, что без TLS проверка имени не включается, а пустой пароль отсутствует."""
    kwargs = redis_connection_kwargs("redis", 6379, "", use_ssl=False)
    assert kwargs["password"] is None
    assert "ssl_check_hostname" not in kwargs
