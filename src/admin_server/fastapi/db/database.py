import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base

POSTGRES_USER = os.getenv("POSTGRES_USER", "financial_ai_agent")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "cool_strong_password")
POSTGRES_DB = os.getenv("POSTGRES_DB", "financial_agent")
DB_HOST = os.getenv("DB_HOST", "postgres-db")

DATABASE_URL = f"postgresql+asyncpg://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{DB_HOST}:5432/{POSTGRES_DB}"

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()

async def get_db():
    async with async_session() as session:
        yield session
