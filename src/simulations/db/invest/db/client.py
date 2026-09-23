import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from functools import lru_cache

# Импортируем Base, чтобы при init_db все модели были привязаны к нему
from src.simulations.db.invest.db.database import Base
from src.core.utils.console_logger import log_info, log_error, log_warning

@lru_cache()
def get_async_engine():
    """
    Ленивая инициализация асинхронного движка базы данных инвестиций.
    """
    DATABASE_URL = os.getenv(
        "INVEST_DATABASE_URL", 
        "postgresql+asyncpg://agent_user:agent_password@localhost:15432/invest_db"
    )
    return create_async_engine(DATABASE_URL, echo=False)

def get_async_session_maker():
    """
    Ленивая инициализация фабрики сессий.
    """
    engine = get_async_engine()
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False
    )

async def init_db():
    """
    Создает все таблицы в базе данных invest_db. (Для симуляции)
    """
    log_warning("Database", "Внимание: Выполняется удаление всех таблиц базы данных!")
    engine = get_async_engine()
    try:
        async with engine.begin() as conn:
            import src.simulations.db.invest.db.models
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        log_info("Invest DB", "Таблицы успешно созданы.")
    except Exception as e:
        log_error("Invest DB", f"Ошибка инициализации БД: {e}")
