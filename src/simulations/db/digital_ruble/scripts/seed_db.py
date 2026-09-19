import os
import asyncio
import random
from datetime import datetime, timedelta
from urllib.parse import urlparse
# pyrefly: ignore [missing-import]
import asyncpg
from dotenv import load_dotenv

load_dotenv()

from sqlalchemy.ext.asyncio import create_async_engine
from src.simulations.db.digital_ruble.db.client import init_db, get_async_session_maker
from src.simulations.db.digital_ruble.db.models import Wallet, RubleTransaction, SmartContract

AsyncSessionLocal = get_async_session_maker()

def random_date_past_months(months=6):
    start_date = datetime.now() - timedelta(days=months*30)
    end_date = datetime.now()
    delta = end_date - start_date
    random_days = random.randrange(delta.days)
    return start_date + timedelta(days=random_days)

def random_date_late_2026():
    # Возвращает дату в ноябре-декабре 2026 года
    start_date = datetime(2026, 11, 1)
    end_date = datetime(2026, 12, 31)
    delta = end_date - start_date
    random_days = random.randrange(delta.days)
    return start_date + timedelta(days=random_days)

async def create_database_if_not_exists():
    DATABASE_URL = os.getenv(
        "RUBLE_DATABASE_URL", 
        "postgresql+asyncpg://agent_user:agent_password@localhost:15432/ruble_db"
    )
    url = DATABASE_URL
    parsed = urlparse(url)
    db_name = parsed.path.lstrip('/')
    
    sys_url = url.replace(f"/{db_name}", "/postgres")
    sys_url_asyncpg = sys_url.replace("postgresql+asyncpg://", "postgres://")
    
    try:
        print(f"Подключаемся к {sys_url_asyncpg} для создания БД {db_name}...")
        conn = await asyncpg.connect(sys_url_asyncpg)
        await conn.execute(f"CREATE DATABASE {db_name}")
        await conn.close()
        print(f"База данных {db_name} успешно создана.")
    except asyncpg.exceptions.DuplicateDatabaseError:
        print(f"База данных {db_name} уже существует.")
    except Exception as e:
        print(f"Ошибка при создании БД: {e}")

async def seed_data():
    print("Создаем таблицы...")
    await init_db()
    
    async with AsyncSessionLocal() as session:
        client_ids = [
            "11111111-1111-1111-1111-111111111111", # Иванов
            "22222222-2222-2222-2222-222222222222", # Петров
            "33333333-3333-3333-3333-333333333333"  # Васильков
        ]
        
        print("Создаем кошельки цифрового рубля (1 на человека)...")
        wallets = []
        for client_id in client_ids:
            wallet = Wallet(
                client_id=client_id,
                wallet_number=str(random.randint(5000000000000000, 5999999999999999)),
                balance=random.uniform(5000, 200000),
                opened_at=random_date_past_months(12)
            )
            wallets.append(wallet)
            session.add(wallet)
            
        await session.flush() # Получаем ID кошельков
        
        print("Создаем историю транзакций...")
        for _ in range(15):
            sender = random.choice(wallets)
            receiver = random.choice([w for w in wallets if w.id != sender.id])
            amount = random.uniform(500, 15000)
            
            tx = RubleTransaction(
                sender_wallet_id=sender.id,
                receiver_wallet_id=receiver.id,
                amount=amount,
                status="completed",
                timestamp=random_date_past_months(3)
            )
            session.add(tx)
            
        print("Создаем ожидающие смарт-контракты...")
        conditions = ["приемка_квартиры", "доставка_товара", "выполнение_услуг", "наступление_даты"]
        
        dummy_code = '''
def execute(ctx):
    # Тестовая заглушка: проверяем статус условия из контекста
    if ctx.get_condition_status() == 'fulfilled':
        ctx.transfer(ctx.creator_id, ctx.receiver_id, ctx.amount)
        return True
    return False
'''

        for _ in range(5):
            creator = random.choice(wallets)
            receiver = random.choice([w for w in wallets if w.id != creator.id])
            amount = random.uniform(10000, 50000)
            
            # Обеспечиваем, чтобы у создателя было достаточно денег для заморозки
            creator.balance += amount
            creator.frozen_balance += amount
            
            sc = SmartContract(
                creator_wallet_id=creator.id,
                receiver_wallet_id=receiver.id,
                amount=amount,
                condition_type=random.choice(conditions),
                contract_code=dummy_code.strip(),
                condition_status="pending",
                status="active",
                error_message=None,
                created_at=random_date_past_months(1),
                executed_at=random_date_late_2026() # Исполнение в конце 2026
            )
            session.add(sc)
            
        await session.commit()
        print("База цифрового рубля успешно заполнена тестовыми данными!")

async def main():
    await create_database_if_not_exists()
    await seed_data()

if __name__ == "__main__":
    asyncio.run(main())
