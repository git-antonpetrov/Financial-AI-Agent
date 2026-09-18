from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine, async_sessionmaker, AsyncSession
from functools import lru_cache
from src.elt.config import get_settings
from src.elt.db.models import Base
from src.core.utils.console_logger import log_info, log_error

@lru_cache
def get_db_engine() -> AsyncEngine:
    """
    Создает и возвращает асинхронный движок базы данных PostgreSQL.
    Использует кэширование для сохранения единственного экземпляра (Singleton).
    
    Returns:
        AsyncEngine: Экземпляр движка SQLAlchemy.
    """
    settings = get_settings()
    log_info("Система БД", "Инициализация асинхронного движка SQLAlchemy...")
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    return engine

@lru_cache
def get_async_session_maker() -> async_sessionmaker[AsyncSession]:
    """
    Возвращает фабрику асинхронных сессий для работы с БД.
    
    Returns:
        async_sessionmaker[AsyncSession]: Фабрика сессий SQLAlchemy.
    """
    engine = get_db_engine()
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def init_db():
    """
    Создает все таблицы в базе данных на основе метаданных моделей.
    Должно вызываться при старте сервиса.
    """
    engine = get_db_engine()
    log_info("Система БД", "Проверка и создание таблиц PostgreSQL...")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        log_info("Система БД", "Таблицы успешно проверены/созданы.")
    except Exception as e:
        log_error("Система БД", f"Ошибка при создании таблиц: {e}")
        raise
