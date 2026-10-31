import os
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.common.db.asyncpg_ssl import build_asyncpg_ssl, database_url_without_ssl_query
from src.simulations.core.utils.console_logger import log_error, log_info, log_warning

# Импортируем Base, чтобы при init_db все модели были привязаны к нему
from src.simulations.db.invest.db.database import Base


@lru_cache
def get_async_engine():
    """
    Ленивая инициализация асинхронного движка базы данных инвестиций.
    """
    DATABASE_URL = os.getenv("INVEST_DATABASE_URL")
    if not DATABASE_URL or not DATABASE_URL.strip():
        raise ValueError(
            "Критическая ошибка конфигурации: переменная окружения INVEST_DATABASE_URL "
            "обязательна, но не задана. Использование значений по умолчанию с открытыми паролями запрещено."
        )
    database_url = database_url_without_ssl_query(DATABASE_URL.strip())
    ssl_arg = build_asyncpg_ssl(os.getenv("POSTGRES_SSLMODE", ""))
    connect_args = {"ssl": ssl_arg} if ssl_arg is not None else {}
    return create_async_engine(database_url, echo=False, connect_args=connect_args)


def get_async_session_maker():
    """
    Ленивая инициализация фабрики сессий.
    """
    engine = get_async_engine()
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)


async def init_db(reset: bool = False):
    """
    Создает таблицы в базе данных invest_db.
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
        log_info("Invest DB", "Таблицы успешно инициализированы.")
    except Exception as e:
        log_error("Invest DB", f"Ошибка инициализации БД: {e}")
        raise
