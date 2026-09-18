import os
import asyncio
from urllib.parse import urlparse
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
            Client(id="11111111-1111-1111-1111-111111111111", full_name="Иванов Иван Иванович", phone_number="+79991112233", password_hash="$2b$12$emr2Q/RPryKNfKPxxGEyy.8q1c.0GuyEPJYygxpe0mpI.aVb1j77i"), # 22222222
            Client(id="22222222-2222-2222-2222-222222222222", full_name="Петров Петр Петрович", phone_number="+79992223344", password_hash="$2b$12$koOzD3tZ7wt5ZvGvUS.aKuyv7FDoFldEQMtpFjp4l4m0Mq6EAT.0e"), # 11111111
            Client(id="33333333-3333-3333-3333-333333333333", full_name="Васильков Василий Васильевич", phone_number="+79993334455", password_hash="$2b$12$1OYYTUao01BlWfPIs5flQ.mYYMOjkyc14n/v8.KSvOaabyWQlgm6.") # 33333333
        ]
        session.add_all(clients)
        await session.commit()
        print("[Server DB] ✅ Сидирование успешно завершено! Клиенты созданы.")

async def run_seed():
    await create_database_if_not_exists()
    await seed_server_data()

if __name__ == "__main__":
    asyncio.run(run_seed())
