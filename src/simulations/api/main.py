import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
sys.path.append(str(Path(__file__).resolve().parents[3]))

import os
import uvicorn
# pyrefly: ignore [missing-import]
from fastapi import FastAPI
# pyrefly: ignore [missing-import]
from fastapi.middleware.cors import CORSMiddleware
from src.simulations.core.utils.console_logger import log_info

# Здесь будут импорты роутеров
from src.simulations.api.routers.bank import router as bank_router
from src.simulations.api.routers.invest import router as invest_router
from src.simulations.api.routers.ruble import router as ruble_router

app = FastAPI(
    title="Financial AI Agent - Simulation API",
    description="""
    Единый API-шлюз симуляции для взаимодействия MCP-серверов (агента) 
    с Банком, Инвестициями и Цифровым рублем.
    Обеспечивает кросс-доменные операции и централизованный доступ к изолированным БД.
    """,
    version="1.0.0"
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
    allow_headers=["Authorization", "Content-Type", "X-Bootstrap-Token", "x-bootstrap-token"],
)

# Подключение роутеров
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
