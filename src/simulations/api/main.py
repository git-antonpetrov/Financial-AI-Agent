import sys
from pathlib import Path

# Добавляет корень проекта в sys.path.
sys.path.append(str(Path(__file__).resolve().parents[3]))

import asyncio
import hmac
import os
from contextlib import asynccontextmanager

# pyrefly: ignore [missing-import]
from fastapi import FastAPI, Request, status

# pyrefly: ignore [missing-import]
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.common.crl import client_serial_is_revoked
from src.common.production_secrets import reject_known_placeholder_secrets
from src.simulations.api.core.auth import run_simulation_revocation_listener
from src.simulations.api.routers.bank import (
    pki_router as bank_pki_router,
)

# Здесь будут импорты роутеров
from src.simulations.api.routers.bank import (
    router as bank_router,
)
from src.simulations.api.routers.invest import router as invest_router
from src.simulations.api.routers.ruble import router as ruble_router
from src.simulations.core.utils.console_logger import log_error, log_info, log_warning
from src.simulations.db.bank.db.client import get_async_engine as get_bank_engine
from src.simulations.db.digital_ruble.db.client import (
    get_async_engine as get_ruble_engine,
)
from src.simulations.db.invest.db.client import get_async_engine as get_invest_engine


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def validate_simulation_security() -> None:
    """Проверяет строгий контур шлюза симуляции перед приёмом запросов.

    При STRICT_SECURITY требует подпись анклава, подпись оракула, непустой
    PROXY_INTERNAL_SECRET и запрещает известные плейсхолдеры секретов.

    Вызывает:
        RuntimeError: Строгий режим включён, а флаг или секрет не соответствует политике.
    """
    strict_sec = os.getenv("STRICT_SECURITY", "true").lower() in ("true", "1", "yes")
    if not strict_sec:
        return
    require_enclave = os.getenv("REQUIRE_ENCLAVE_SIGNATURE", "true").lower() in (
        "true",
        "1",
        "yes",
    )
    if not require_enclave:
        raise RuntimeError("STRICT_SECURITY: REQUIRE_ENCLAVE_SIGNATURE must be true in production")
    require_oracle = os.getenv("REQUIRE_ORACLE_SIGNATURE", "true").lower() in (
        "true",
        "1",
        "yes",
    )
    if not require_oracle:
        raise RuntimeError("STRICT_SECURITY: REQUIRE_ORACLE_SIGNATURE must be true in production")
    proxy_secret = os.getenv("PROXY_INTERNAL_SECRET", "").strip()
    if not proxy_secret:
        raise RuntimeError("STRICT_SECURITY: PROXY_INTERNAL_SECRET must be set")
    reject_known_placeholder_secrets()


def request_has_valid_proxy_secret(
    path: str,
    client_host: str | None,
    presented_secret: str | None,
) -> bool:
    """Сообщает, пропускает ли шлюз запрос с данным секретом прокси.

    Адрес loopback на /health проходит без секрета, потому что контейнер так
    проверяет собственную работоспособность. Остальные запросы требуют точное
    совпадение заголовка X-Proxy-Secret с PROXY_INTERNAL_SECRET.

    Аргументы:
        path: Путь запроса.
        client_host: Адрес TCP-клиента. Пустое значение не считается loopback.
        presented_secret: Значение заголовка X-Proxy-Secret.

    Возвращает:
        True, если запрос можно передать в обработчик.
    """
    if path == "/health" and (client_host or "") in _LOOPBACK_HOSTS:
        return True
    expected = os.getenv("PROXY_INTERNAL_SECRET", "").strip()
    presented = (presented_secret or "").strip()
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented, expected)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Запускает проверку строгого контура и слушатель отзывов Redis.

    Побочные эффекты:
        Поднимает фоновую задачу Pub/Sub и закрывает пулы PostgreSQL при остановке.
    """
    validate_simulation_security()

    # Фоновая задача мгновенной синхронизации отзывов через Redis Pub/Sub (SEC-09)
    listener_task = None
    should_run_listener = os.getenv("ENABLE_REVOCATION_LISTENER", "true").lower() in ("true", "1")
    if should_run_listener:
        try:
            listener_task = asyncio.create_task(run_simulation_revocation_listener())
            log_info("Simulation API", "Фоновый слушатель отзывов Redis запущен")
        except Exception as e:
            log_warning("Simulation API", f"Не удалось запустить фоновый слушатель отзывов: {e}")

    log_info("Simulation API", "Simulation API запущен и готов к работе")
    yield

    if listener_task:
        listener_task.cancel()
        try:
            await listener_task
        except asyncio.CancelledError:
            pass

    # Корректное освобождение пулов соединений PostgreSQL (SEC-18)
    for engine_fn, name in [
        (get_bank_engine, "Bank DB"),
        (get_invest_engine, "Invest DB"),
        (get_ruble_engine, "Digital Ruble DB"),
    ]:
        try:
            engine = engine_fn()
            await engine.dispose()
            log_info(
                "Simulation API",
                f"Пул соединений {name} успешно освобожден (engine.dispose)",
            )
        except Exception as e:
            log_error("Simulation API", f"Ошибка при освобождении пула {name}: {e}")


app = FastAPI(
    title="Financial AI Agent - Simulation API",
    description="""
    Единый API-шлюз симуляции для взаимодействия MCP-серверов (агента)
    с Банком, Инвестициями и Цифровым рублем.
    Обеспечивает кросс-доменные операции и централизованный доступ к изолированным БД.
    """,
    version="1.0.0",
    lifespan=lifespan,
)

cors_env = os.getenv("CORS_ALLOWED_ORIGINS", "")
if cors_env.strip():
    allowed_origins = [o.strip() for o in cors_env.split(",") if o.strip()]
else:
    allowed_origins = [
        "http://localhost:8001",
        "http://localhost:8002",
        "http://localhost:5173",
        "tauri://localhost",
        "https://tauri.localhost",
        "http://tauri.localhost",
        "asset://localhost",
        "https://admin.fin-ai-agent.ru",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Bootstrap-Token",
        "x-bootstrap-token",
        "X-Enclave-Signature",
        "x-enclave-signature",
        "X-Agent-Cert",
        "x-agent-cert",
        "X-Enclave-Cert",
        "x-enclave-cert",
        "X-Signature",
        "x-signature",
        "X-Cert",
        "x-cert",
        "X-Nonce",
        "x-nonce",
        "X-Timestamp",
        "x-timestamp",
        "X-Bank-Signature",
        "x-bank-signature",
    ],
    expose_headers=[
        "X-Bank-Signature",
        "X-Bank-Cert-Fingerprint",
        "X-Bank-Cert-Serial",
        "X-Nonce",
        "X-Timestamp",
    ],
)


@app.middleware("http")
async def proxy_secret_middleware(request: Request, call_next):
    """Отклоняет прямой вызов шлюза без верного секрета обратного прокси.

    Секрет закрывает обращение мимо Caddy. Проверка /health с loopback остаётся
    без секрета, чтобы healthcheck контейнера не требовал заголовок. Если прокси
    передал серийный номер, шлюз сверяет его с подписанным списком отзыва.
    """
    host = request.client.host if request.client else None
    presented = request.headers.get("x-proxy-secret")
    if not request_has_valid_proxy_secret(request.url.path, host, presented):
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Неверный или отсутствующий X-Proxy-Secret"},
        )
    revoked = rejected_revoked_client(request)
    if revoked is not None:
        return revoked
    return await call_next(request)


def rejected_revoked_client(request: Request) -> JSONResponse | None:
    """Возвращает отказ, если серийный номер клиентского сертификата отозван.

    Аргументы:
        request: Входящий запрос шлюза.

    Возвращает:
        Ответ 401 или 503. ``None``, если заголовок серийного номера отсутствует
        или список отзыва не содержит этот номер.
    """
    serial = request.headers.get("x-client-cert-serial")
    if not serial:
        return None
    strict = os.getenv("STRICT_SECURITY", "true").lower() in ("true", "1", "yes")
    try:
        if client_serial_is_revoked(serial):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Клиентский сертификат отозван"},
            )
    except FileNotFoundError:
        if strict:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": "Список отзыва сертификатов недоступен"},
            )
    except ValueError:
        if strict:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": "Список отзыва сертификатов недействителен"},
            )
    return None


# Подключение роутеров
app.include_router(bank_pki_router, prefix="/bank", tags=["Bank PKI"])
app.include_router(bank_router, prefix="/bank", tags=["Bank"])
app.include_router(invest_router, prefix="/invest", tags=["Invest"])
app.include_router(ruble_router, prefix="/ruble", tags=["Digital Ruble"])


@app.get("/health")
async def health_check():
    """
    Эндпоинт для проверки жизнеспособности (health check) API.
    """
    return {"status": "ok", "message": "Simulation API is running"}


if __name__ == "__main__":
    from src.simulations.api.serve import main

    main()
