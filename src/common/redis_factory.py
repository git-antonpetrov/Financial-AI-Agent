"""Собирает клиент Redis с коротким таймаутом установки соединения."""

import os
from typing import Any


def redis_connection_kwargs(
    host: str,
    port: int,
    password: str | None = None,
    use_ssl: bool = False,
    ca_file: str | None = None,
) -> dict[str, Any]:
    """Возвращает аргументы redis.Redis с таймаутом соединения не длиннее двух секунд.

    При включённом TLS сверяет имя host с сертификатом Redis и сохраняет пароль.

    Аргументы:
        host: Имя или адрес сервера Redis. При TLS это имя сверяется с сертификатом.
        port: Порт Redis.
        password: Пароль. Пустая строка означает отсутствие пароля.
        use_ssl: Включает TLS и проверку имени хоста.
        ca_file: Путь к корневому сертификату, если TLS включён.

    Возвращает:
        Словарь аргументов конструктора redis.Redis.
    """
    raw_timeout = os.getenv("REDIS_CONNECT_TIMEOUT", "2")
    try:
        timeout = min(2.0, float(raw_timeout))
    except ValueError:
        timeout = 2.0
    if timeout <= 0:
        timeout = 2.0
    kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "password": password or None,
        "decode_responses": True,
        "socket_timeout": timeout,
        "socket_connect_timeout": timeout,
        "retry_on_timeout": False,
    }
    try:
        from redis.backoff import NoBackoff
        from redis.retry import Retry

        kwargs["retry"] = Retry(NoBackoff(), 0)
    except Exception:
        pass
    if use_ssl:
        kwargs["ssl"] = True
        kwargs["ssl_check_hostname"] = True
        if ca_file and os.path.isfile(ca_file):
            kwargs["ssl_ca_certs"] = ca_file
            kwargs["ssl_cert_reqs"] = "required"
    return kwargs


def open_redis_or_none(**kwargs: Any) -> Any | None:
    """Открывает Redis и возвращает None, если сервер не ответил за таймаут.

    Аргументы:
        kwargs: Аргументы redis.Redis, обычно результат redis_connection_kwargs.

    Возвращает:
        Клиент Redis либо None, если соединение не установлено.
    """
    import redis

    client = redis.Redis(**kwargs)
    try:
        client.ping()
    except Exception:
        try:
            client.close()
        except Exception:
            pass
        return None
    return client
