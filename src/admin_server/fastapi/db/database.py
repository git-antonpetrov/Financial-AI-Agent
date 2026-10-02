import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base

from urllib.parse import quote_plus

POSTGRES_USER = os.getenv("POSTGRES_USER", "financial_ai_agent")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD")
if not POSTGRES_PASSWORD:
    raise ValueError("CRITICAL CONFIGURATION ERROR: POSTGRES_PASSWORD environment variable is required but not set.")
POSTGRES_DB = os.getenv("POSTGRES_DB", "financial_agent")
DB_HOST = os.getenv("DB_HOST", "postgres-db")

encoded_password = quote_plus(POSTGRES_PASSWORD)
DATABASE_URL = f"postgresql+asyncpg://{POSTGRES_USER}:{encoded_password}@{DB_HOST}:5432/{POSTGRES_DB}"

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
    async with async_session() as session:
        yield session
