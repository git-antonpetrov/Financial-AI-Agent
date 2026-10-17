import asyncio
import os
import random
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import urlparse

# pyrefly: ignore [missing-import]
import asyncpg
from dotenv import load_dotenv

load_dotenv()

from src.simulations.db.digital_ruble.db.client import get_async_session_maker, init_db
from src.simulations.db.digital_ruble.db.models import (
    RubleTransaction,
    SmartContract,
    Wallet,
)

AsyncSessionLocal = get_async_session_maker()


def random_date_past_months(months=6):
    start_date = datetime.now() - timedelta(days=months * 30)
    end_date = datetime.now()
    delta = end_date - start_date
    random_days = random.randrange(delta.days)
    return start_date + timedelta(days=random_days)


from src.simulations.core.utils.db_utils import validate_db_name


async def create_database_if_not_exists():
    url = os.getenv("RUBLE_DATABASE_URL") or os.getenv("DIGITAL_RUBLE_DATABASE_URL")
    if not url or not url.strip():
        raise ValueError(
            "Критическая ошибка конфигурации: переменная окружения RUBLE_DATABASE_URL "
            "обязательна, но не задана. Использование значений по умолчанию запрещено."
        )
    url = url.strip()
    parsed = urlparse(url.replace("postgresql+asyncpg://", "postgresql://"))
    raw_db_name = parsed.path.lstrip("/")
    db_name = validate_db_name(raw_db_name)

    sys_url = url.replace(f"/{raw_db_name}", "/postgres")
    sys_url_asyncpg = sys_url.replace("postgresql+asyncpg://", "postgres://")

    ssl_mode = os.getenv("POSTGRES_SSLMODE", "").strip()
    conn_kwargs = {}
    if ssl_mode and ssl_mode not in ("disable", "allow"):
        if os.path.exists("/certs/ca.crt"):
            import ssl
            conn_kwargs["ssl"] = ssl.create_default_context(cafile="/certs/ca.crt")
        else:
            conn_kwargs["ssl"] = True

    from src.simulations.core.utils.console_logger import log_info, log_warning

    try:
        log_info("Digital Ruble DB", f"Подключаемся к {sys_url_asyncpg} для создания БД {db_name}...")
        conn = await asyncpg.connect(sys_url_asyncpg, **conn_kwargs)
        await conn.execute(f'CREATE DATABASE "{db_name}"')
        await conn.close()
        log_info("Digital Ruble DB", f"База данных {db_name} успешно создана.")
    except asyncpg.exceptions.DuplicateDatabaseError:
        log_info("Digital Ruble DB", f"База данных {db_name} уже существует.")
    except Exception as e:
        log_warning("Digital Ruble DB", f"Ошибка при создании БД: {e}")


async def seed_data():
    from src.simulations.core.utils.console_logger import log_info
    log_info("Digital Ruble DB", "Создаем таблицы...")
    await init_db()

    async with AsyncSessionLocal() as session:
        client_ids = [
            "11111111-1111-1111-1111-111111111111",  # Иванов
            "22222222-2222-2222-2222-222222222222",  # Петров
            "33333333-3333-3333-3333-333333333333",  # Васильков
        ]

        log_info("Digital Ruble DB", "Создаем кошельки цифрового рубля (1 на человека)...")
        wallets = []
        for client_id in client_ids:
            wallet = Wallet(
                client_id=client_id,
                wallet_number=str(random.randint(5000000000000000, 5999999999999999)),
                balance=Decimal(str(round(random.uniform(5000, 200000), 2))),
                opened_at=random_date_past_months(12),
            )
            wallets.append(wallet)
            session.add(wallet)

        await session.flush()  # Получаем ID кошельков

        log_info("Digital Ruble DB", "Создаем историю транзакций...")
        for _ in range(15):
            sender = random.choice(wallets)
            receiver = random.choice([w for w in wallets if w.id != sender.id])
            amount = Decimal(str(round(random.uniform(500, 15000), 2)))

            tx = RubleTransaction(
                sender_wallet_id=sender.id,
                receiver_wallet_id=receiver.id,
                amount=amount,
                status="completed",
                timestamp=random_date_past_months(3),
            )
            session.add(tx)

        log_info("Digital Ruble DB", "Создаем ожидающие смарт-контракты...")
        conditions = [
            "приемка_квартиры",
            "доставка_товара",
            "выполнение_услуг",
            "наступление_даты",
        ]

        template_contract_code = """
def execute(ctx):
    # Базовый шаблон смарт-контракта: проверка статуса внешнего условия из контекста
    if ctx.get_condition_status() == 'fulfilled':
        ctx.transfer(ctx.creator_id, ctx.receiver_id, ctx.amount)
        return True
    return False
"""

        for _ in range(5):
            creator = random.choice(wallets)
            receiver = random.choice([w for w in wallets if w.id != creator.id])
            amount = Decimal(str(round(random.uniform(10000, 50000), 2)))

            # Обеспечиваем, чтобы у создателя было достаточно денег для заморозки
            creator.balance = Decimal(str(creator.balance)) + amount
            creator.frozen_balance = Decimal(str(creator.frozen_balance)) + amount

            sc = SmartContract(
                creator_wallet_id=creator.id,
                receiver_wallet_id=receiver.id,
                amount=amount,
                condition_type=random.choice(conditions),
                contract_code=template_contract_code.strip(),
                condition_status="pending",
                status="active",
                error_message=None,
                created_at=random_date_past_months(1),
                executed_at=None,  # Исполняется воркером
            )
            session.add(sc)

        await session.commit()
        log_info("Digital Ruble DB", "База цифрового рубля успешно заполнена тестовыми данными!")


async def main():
    await create_database_if_not_exists()
    await seed_data()


if __name__ == "__main__":
    asyncio.run(main())
