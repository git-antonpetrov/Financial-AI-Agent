import os
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.simulations.core.utils.console_logger import log_error, log_info, log_warning

# Импортируем Base, чтобы при init_db все модели были привязаны к нему
from src.simulations.db.digital_ruble.db.database import Base


@lru_cache
def get_async_engine():
    """
    Ленивая инициализация асинхронного движка базы данных цифрового рубля.
    """
    DATABASE_URL = os.getenv("RUBLE_DATABASE_URL") or os.getenv("DIGITAL_RUBLE_DATABASE_URL")
    if not DATABASE_URL or not DATABASE_URL.strip():
        raise ValueError(
            "Критическая ошибка конфигурации: переменная окружения RUBLE_DATABASE_URL "
            "обязательна, но не задана. Использование значений по умолчанию с открытыми паролями запрещено."
        )
    DATABASE_URL = DATABASE_URL.strip()
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
    Создает таблицы в базе данных digital_db.
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
        log_info("Digital Ruble DB", "Таблицы успешно инициализированы.")
    except Exception as e:
        log_error("Digital Ruble DB", f"Ошибка инициализации БД: {e}")
