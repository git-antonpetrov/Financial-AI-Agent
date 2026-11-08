"""Запускает шлюз симуляции по TLS сертификатом банка."""

import os
import ssl
from collections.abc import Callable

import uvicorn


def tls_material() -> tuple[str, str]:
    """Возвращает пути сертификата и ключа шлюза.

    Возвращает:
        Пара из пути сертификата и пути ключа.

    Вызывает:
        SystemExit: Файл сертификата или ключа отсутствует.
    """
    certificate = os.getenv("SIMULATION_TLS_CERT", "/certs/bank.crt")
    key = os.getenv("SIMULATION_TLS_KEY", "/certs/bank.key")
    if not os.path.isfile(certificate) or not os.path.isfile(key):
        raise SystemExit("Сертификат шлюза симуляции не найден")
    return certificate, key


def restrict_to_tls_13(context: ssl.SSLContext) -> ssl.SSLContext:
    """Ограничивает серверный контекст протоколом TLS 1.3.

    Аргументы:
        context: Контекст, в который uvicorn уже загрузил цепочку сертификата.

    Возвращает:
        Тот же контекст с минимальной версией TLS 1.3.
    """
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    return context


def build_server(certificate: str, key: str) -> uvicorn.Server:
    """Собирает сервер uvicorn с сертификатом банка.

    Аргументы:
        certificate: Путь к bank.crt.
        key: Путь к bank.key.

    Возвращает:
        Сервер, который слушает порт 8002 только по TLS.
    """

    def factory(_config: uvicorn.Config, default_factory: Callable[[], ssl.SSLContext]) -> ssl.SSLContext:
        return restrict_to_tls_13(default_factory())

    config = uvicorn.Config(
        "src.simulations.api.main:app",
        host="0.0.0.0",
        port=8002,
        ssl_certfile=certificate,
        ssl_keyfile=key,
        ssl_context_factory=factory,
    )
    return uvicorn.Server(config)


def main() -> None:
    """Запускает шлюз. Без сертификата процесс завершается."""
    certificate, key = tls_material()
    build_server(certificate, key).run()


if __name__ == "__main__":
    main()
