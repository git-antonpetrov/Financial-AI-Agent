import os
import asyncio
import random
from datetime import datetime, timedelta, date
from urllib.parse import urlparse
# pyrefly: ignore [missing-import]
import asyncpg
from dotenv import load_dotenv
load_dotenv()

from sqlalchemy.ext.asyncio import create_async_engine
from src.mcp_servers.bank.db.database import Base, AsyncSessionLocal, engine, init_db
from src.mcp_servers.bank.db.models import Client, Account, Card, Transaction, Tariff, AutoPayment, PaymentReminder

def random_date_past_months(months=3):
    start_date = datetime.now() - timedelta(days=months*30)
    end_date = datetime.now()
    delta = end_date - start_date
    random_days = random.randrange(delta.days)
    random_seconds = random.randrange(24*60*60)
    return start_date + timedelta(days=random_days, seconds=random_seconds)

async def create_database_if_not_exists():
    url = os.getenv("BANK_DATABASE_URL", "postgresql+asyncpg://agent_user:agent_password@localhost:15432/bank_db")
    parsed = urlparse(url.replace("postgresql+asyncpg://", "postgresql://"))
    db_name = parsed.path.lstrip("/")
    
    # URL для подключения к системной базе postgres
    sys_url = url.replace(f"/{db_name}", "/postgres")
    sys_url_asyncpg = sys_url.replace("postgresql+asyncpg://", "postgres://")
    
    try:
        print(f"Подключаемся к {sys_url_asyncpg} для создания БД {db_name}...")
        conn = await asyncpg.connect(sys_url_asyncpg)
        await conn.execute(f"CREATE DATABASE {db_name}")
        await conn.close()
        print(f"✅ База данных {db_name} успешно создана.")
    except asyncpg.exceptions.DuplicateDatabaseError:
        print(f"ℹ️ База данных {db_name} уже существует.")
    except Exception as e:
        print(f"⚠️ Ошибка при создании БД (возможно она уже есть или нет прав): {e}")

async def seed_data():
    print("Создаем таблицы...")
    await init_db()
    
    async with AsyncSessionLocal() as session:
        # Проверяем, есть ли уже данные (чтобы не дублировать)
        # Если хотим пересоздавать — можно добавить Base.metadata.drop_all(engine) в init_db
        
        # 1. Создаем тарифы
        print("Создаем тарифы...")
        tariffs_data = [
            Tariff(name="Базовый", service_cost=100, non_sbp_limit=20000, sbp_limit=30000000, cash_withdrawal_limit=100000, cash_withdrawal_fee=100, transfer_over_limit_fee_percent=2.0, deposit_fee=0),
            Tariff(name="Зарплатный", service_cost=0, non_sbp_limit=20000, sbp_limit=30000000, cash_withdrawal_limit=100000, cash_withdrawal_fee=100, transfer_over_limit_fee_percent=2.0, deposit_fee=0),
            Tariff(name="Льготный", service_cost=0, non_sbp_limit=0, sbp_limit=30000000, cash_withdrawal_limit=15000, cash_withdrawal_fee=100, transfer_over_limit_fee_percent=2.0, deposit_fee=0),
        ]
        session.add_all(tariffs_data)
        await session.commit()
        
        # 2. Создаем клиентов
        print("Создаем клиентов...")
        clients = [
            Client(full_name="Иванов Иван Иванович", phone_number="+79991112233"),
            Client(full_name="Петров Петр Петрович", phone_number="+79992223344"),
            Client(full_name="Васильков Василий Васильевич", phone_number="+79993334455")
        ]
        session.add_all(clients)
        await session.commit()
        
        # 3. Создаем счета, карты и транзакции для каждого
        print("Создаем счета, карты и транзакции...")
        
        operation_types_expense = ['transfer_sbp', 'transfer_non_sbp', 'purchase']
        operation_types_income = ['transfer_sbp', 'salary', 'cash_deposit', 'top_up']
        merchants = ["Кофемания", "Пятерочка", "Яндекс.Такси", "Аптека", "АЗС Лукойл", "ВкусВилл", "Ресторан"]
        
        for client in clients:
            # Даем каждому клиенту 1-2 счета
            num_accounts = random.randint(1, 2)
            for _ in range(num_accounts):
                tariff = random.choice(tariffs_data)
                account = Account(
                    client_id=client.id,
                    account_number=str(random.randint(40817810000000000000, 40817810099999999999)),
                    balance=random.uniform(10000, 1500000),
                    currency="RUB",
                    tariff_id=tariff.id
                )
                session.add(account)
                await session.flush() # Получаем account.id
                
                # Карты для счета
                num_cards = random.randint(1, 3)
                for _ in range(num_cards):
                    card = Card(
                        account_id=account.id,
                        card_number=str(random.randint(2202000000000000, 2202999999999999)),
                        status=random.choices(["active", "blocked", "frozen"], weights=[0.8, 0.1, 0.1])[0]
                    )
                    session.add(card)
                
                # 30-50 транзакций
                num_tx = random.randint(30, 50)
                for _ in range(num_tx):
                    is_income = random.choice([True, False])
                    tx_date_time = random_date_past_months(3)
                    
                    if is_income:
                        category = "income"
                        op_type = random.choice(operation_types_income)
                        amount = random.uniform(1000, 150000)
                        desc = f"Поступление: {op_type}"
                    else:
                        category = "expense"
                        op_type = random.choice(operation_types_expense)
                        amount = random.uniform(100, 15000) * -1
                        desc = f"Оплата: {random.choice(merchants)}" if op_type == "purchase" else f"Перевод: {op_type}"
                    
                    commission = 0.00
                    if category == "expense" and op_type == "transfer_non_sbp" and random.random() > 0.5:
                        commission = amount * -0.02 # 2% комиссия
                        
                    tx = Transaction(
                        account_id=account.id,
                        category=category,
                        operation_type=op_type,
                        amount=round(amount, 2),
                        commission=round(commission, 2),
                        description=desc,
                        status=random.choices(["completed", "failed", "pending"], weights=[0.9, 0.05, 0.05])[0],
                        date=tx_date_time.date(),
                        time=tx_date_time.time()
                    )
                    session.add(tx)
                    
        await session.commit()
        print("✅ Сидирование успешно завершено! Данные сгенерированы.")

async def main():
    await create_database_if_not_exists()
    await seed_data()

if __name__ == "__main__":
    asyncio.run(main())
