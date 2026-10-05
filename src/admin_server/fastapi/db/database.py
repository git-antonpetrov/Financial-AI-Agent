import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base

from urllib.parse import quote_plus

POSTGRES_USER = os.getenv("POSTGRES_USER", "financial_ai_agent")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")
POSTGRES_DB = os.getenv("POSTGRES_DB", "financial_agent")
DB_HOST = os.getenv("DB_HOST", "postgres-db")
POSTGRES_SSLMODE = os.getenv("POSTGRES_SSLMODE", "").strip()

def validate_database_env() -> None:
    """Проверяет наличие обязательных параметров подключения к базе данных PostgreSQL."""
    if not POSTGRES_PASSWORD:
        raise ValueError("Критическая ошибка конфигурации: переменная окружения POSTGRES_PASSWORD обязательна, но не задана.")

encoded_password = quote_plus(POSTGRES_PASSWORD) if POSTGRES_PASSWORD else ""
DATABASE_URL = f"postgresql+asyncpg://{POSTGRES_USER}:{encoded_password}@{DB_HOST}:5432/{POSTGRES_DB}"
if POSTGRES_SSLMODE:
    DATABASE_URL += f"?ssl={POSTGRES_SSLMODE}"

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    pool_recycle=3600,
    pool_timeout=30.0
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()

async def get_db():
    """Создает и возвращает асинхронную сессию базы данных PostgreSQL."""
    async with async_session() as session:
        yield session
