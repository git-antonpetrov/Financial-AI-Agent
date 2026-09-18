import os
import asyncio
from urllib.parse import urlparse
# pyrefly: ignore [missing-import]
import asyncpg
from dotenv import load_dotenv

load_dotenv()

from src.server.db.database import AsyncSessionLocal, init_db
from src.server.db.models import Client

async def create_database_if_not_exists():
    url = os.getenv("SERVER_DATABASE_URL", "postgresql+asyncpg://agent_user:agent_password@localhost:15432/server_db")
    parsed = urlparse(url.replace("postgresql+asyncpg://", "postgresql://"))
    db_name = parsed.path.lstrip("/")
    
    sys_url = url.replace(f"/{db_name}", "/postgres")
    sys_url_asyncpg = sys_url.replace("postgresql+asyncpg://", "postgres://")
    
    try:
        print(f"[Server DB] Подключаемся к {sys_url_asyncpg} для создания БД {db_name}...")
        conn = await asyncpg.connect(sys_url_asyncpg)
        await conn.execute(f"CREATE DATABASE {db_name}")
        await conn.close()
        print(f"[Server DB] ✅ База данных {db_name} успешно создана.")
    except asyncpg.exceptions.DuplicateDatabaseError:
        print(f"[Server DB] ℹ️ База данных {db_name} уже существует.")
    except Exception as e:
        print(f"[Server DB] ⚠️ Ошибка при создании БД: {e}")

async def seed_server_data():
    print("[Server DB] Создаем таблицы...")
    await init_db()
    
    async with AsyncSessionLocal() as session:
        print("[Server DB] Создаем клиентов...")
        clients = [
            Client(id="3f8a0d4c-21a4-4a25-9980-692a8e8f81f1", full_name="Иванов Иван Иванович", phone_number="+79991112233", password_hash="$2b$12$emr2Q/RPryKNfKPxxGEyy.8q1c.0GuyEPJYygxpe0mpI.aVb1j77i"),
            Client(id="7d3c5f42-4211-4091-a8d2-4cf01826f09e", full_name="Петров Петр Петрович", phone_number="+79992223344", password_hash="$2b$12$koOzD3tZ7wt5ZvGvUS.aKuyv7FDoFldEQMtpFjp4l4m0Mq6EAT.0e"),
            Client(id="b9a13d7e-91c2-401d-8f90-1c39054316a7", full_name="Васильков Василий Васильевич", phone_number="+79993334455", password_hash="$2b$12$1OYYTUao01BlWfPIs5flQ.mYYMOjkyc14n/v8.KSvOaabyWQlgm6.")
        ]
        session.add_all(clients)
        await session.commit()
        print("[Server DB] ✅ Сидирование успешно завершено! Клиенты созданы.")

async def run_seed():
    await create_database_if_not_exists()
    await seed_server_data()

if __name__ == "__main__":
    asyncio.run(run_seed())
