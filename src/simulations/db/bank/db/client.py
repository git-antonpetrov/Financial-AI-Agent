import os
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.simulations.core.utils.console_logger import log_error, log_info, log_warning

# Импортируем Base, чтобы при init_db все модели были привязаны к нему
from src.simulations.db.bank.db.database import Base


@lru_cache
def get_async_engine():
    """
    Создает и возвращает асинхронный движок SQLAlchemy для базы данных банка.
    """
    DATABASE_URL = os.getenv(
        "BANK_DATABASE_URL",
        "postgresql+asyncpg://agent_user:agent_password@localhost:15432/bank_db",
    )
    ssl_mode = os.getenv("POSTGRES_SSLMODE", "").strip()
    if ssl_mode and "?" not in DATABASE_URL:
        DATABASE_URL += f"?ssl={ssl_mode}"
    return create_async_engine(DATABASE_URL, echo=False)


def get_async_session_maker():
    """
    Ленивая инициализация фабрики сессий.
    """
    engine = get_async_engine()
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)


async def init_db(reset: bool = False):
    """
    Создает таблицы в базе данных bank_db.
    Удаление существующих таблиц выполняется только при явном флаге reset=True
    или переменной окружения DROP_EXISTING_TABLES=true.
    """
    should_drop = reset or os.getenv("DROP_EXISTING_TABLES", "false").lower() in ("true", "1")
    engine = get_async_engine()
    try:
        async with engine.begin() as conn:
            if should_drop:
                log_warning("Database", "Внимание: Выполняется пересоздание всех таблиц базы данных!")
                await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        log_info("Bank DB", "Таблицы успешно инициализированы.")
    except Exception as e:
        log_error("Bank DB", f"Ошибка инициализации БД: {e}")
