import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base

# Получаем URL из окружения
DATABASE_URL = os.getenv(
    "SERVER_DATABASE_URL", 
    "postgresql+asyncpg://agent_user:agent_password@localhost:15432/server_db"
)

# Асинхронный движок
engine = create_async_engine(DATABASE_URL, echo=False)

# Фабрика сессий
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False
)

# Базовый класс для моделей
Base = declarative_base()

async def init_db():
    """
    Создает все таблицы в базе данных server_db.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
