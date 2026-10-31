"""Собирает параметры TLS для клиентов PostgreSQL.

Режим libpq ``require`` шифрует канал и не проверяет сертификат сервера.
Спецификация канала требует сверку с корневым сертификатом, поэтому
``require`` при наличии файла CA превращается в проверку ``verify-full``.
"""

import os
import ssl
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def database_url_without_ssl_query(database_url: str) -> str:
    """Удаляет параметр ``ssl`` из URL SQLAlchemy.

    asyncpg получает этот параметр как строку и не принимает значение
    libpq ``require``. Режим TLS передаётся отдельным контекстом.

    Аргументы:
        database_url: URL подключения ``postgresql+asyncpg``.

    Возвращает:
        Тот же URL без query-параметра ``ssl``.
    """
    parts = urlsplit(database_url)
    if not parts.query:
        return database_url
    kept = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key.lower() != "ssl"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def _ca_path(ca_file: str | None) -> str:
    """Возвращает путь к корневому сертификату клиента PostgreSQL."""
    candidate = (ca_file or os.getenv("SSL_CERT_FILE") or "/certs/ca.crt").strip()
    return candidate


def _context_for_mode(sslmode: str, ca_path: str) -> ssl.SSLContext:
    """Создаёт контекст TLS, который проверяет сертификат сервера по CA.

    Аргументы:
        sslmode: Нормализованный режим ``require``, ``verify-ca`` или ``verify-full``.
        ca_path: Существующий файл корневого сертификата.

    Возвращает:
        Контекст с ``CERT_REQUIRED``. Имя хоста проверяется в режиме ``verify-full``
        и в режиме ``require``, потому что сертификат сервиса содержит DNS-имя контейнера.
    """
    context = ssl.create_default_context(cafile=ca_path)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = sslmode in ("require", "verify-full")
    return context


def build_asyncpg_ssl(sslmode: str | None, ca_file: str | None = None) -> ssl.SSLContext | None:
    """Возвращает аргумент ``ssl`` для ``asyncpg.connect``.

    Аргументы:
        sslmode: Значение ``POSTGRES_SSLMODE``. Пустая строка означает ``verify-full``.
            ``disable`` и ``allow`` оставляют канал без обязательного TLS.
        ca_file: Путь к ``ca.crt``. По умолчанию читается ``SSL_CERT_FILE``.

    Возвращает:
        Контекст TLS либо ``None``, если шифрование не требуется.

    Вызывает:
        RuntimeError: Режим требует проверки сертификата, а файл CA отсутствует.
    """
    normalized = (sslmode or "").strip().lower()
    if normalized in ("disable", "allow"):
        return None
    if normalized == "":
        normalized = "verify-full"

    ca_path = _ca_path(ca_file)
    if normalized == "prefer":
        if os.path.isfile(ca_path):
            return _context_for_mode("verify-full", ca_path)
        return None

    if normalized not in ("require", "verify-ca", "verify-full"):
        raise RuntimeError(f"Неизвестный режим POSTGRES_SSLMODE: {sslmode}")

    if not os.path.isfile(ca_path):
        raise RuntimeError(
            "POSTGRES_SSLMODE требует проверку сертификата сервера, "
            f"но файл CA не найден: {ca_path}"
        )
    return _context_for_mode(normalized, ca_path)


def libpq_sslmode(sslmode: str | None, ca_file: str | None = None) -> str | None:
    """Возвращает режим libpq, в котором клиент проверяет ``ca.crt``.

    Аргументы:
        sslmode: Исходное значение ``POSTGRES_SSLMODE``.
        ca_file: Путь к корневому сертификату.

    Возвращает:
        Режим для psycopg. Пустая строка означает ``verify-full``.
        ``require`` заменяется на ``verify-full``, если файл CA есть.
        ``None``, если задан ``disable``.
    """
    normalized = (sslmode or "").strip().lower()
    if normalized == "disable":
        return None
    if normalized == "":
        return "verify-full"
    if normalized == "require" and os.path.isfile(_ca_path(ca_file)):
        return "verify-full"
    return normalized
