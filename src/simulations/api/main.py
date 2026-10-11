import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
sys.path.append(str(Path(__file__).resolve().parents[3]))

import os
from contextlib import asynccontextmanager

import uvicorn

# pyrefly: ignore [missing-import]
from fastapi import FastAPI

# pyrefly: ignore [missing-import]
from fastapi.middleware.cors import CORSMiddleware

from src.simulations.api.routers.bank import (
    pki_router as bank_pki_router,
)

# Здесь будут импорты роутеров
from src.simulations.api.routers.bank import (
    router as bank_router,
)
from src.simulations.api.routers.invest import router as invest_router
from src.simulations.api.routers.ruble import router as ruble_router
from src.simulations.core.utils.console_logger import log_error, log_info
from src.simulations.db.bank.db.client import get_async_engine as get_bank_engine
from src.simulations.db.digital_ruble.db.client import (
    get_async_engine as get_ruble_engine,
)
from src.simulations.db.invest.db.client import get_async_engine as get_invest_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Валидация строгого режима безопасности (SEC-10)
    strict_sec = os.getenv("STRICT_SECURITY", "false").lower() in ("true", "1")
    if strict_sec:
        require_enclave = os.getenv("REQUIRE_ENCLAVE_SIGNATURE", "true").lower() in (
            "true",
            "1",
        )
        if not require_enclave:
            raise RuntimeError("STRICT_SECURITY: REQUIRE_ENCLAVE_SIGNATURE must be true in production")
        require_oracle = os.getenv("REQUIRE_ORACLE_SIGNATURE", "true").lower() in (
            "true",
            "1",
        )
        if not require_oracle:
            raise RuntimeError("STRICT_SECURITY: REQUIRE_ORACLE_SIGNATURE must be true in production")

    log_info("Simulation API", "Simulation API запущен и готов к работе")
    yield

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
        "X-Nonce",
        "x-nonce",
        "X-Timestamp",
        "x-timestamp",
        "X-Bank-Signature",
    ],
    expose_headers=["X-Bank-Signature", "X-Nonce", "X-Timestamp"],
)

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
    log_info("Simulation API", "Запуск сервера API...")
    uvicorn.run("main:app", host="0.0.0.0", port=8002, reload=True)
