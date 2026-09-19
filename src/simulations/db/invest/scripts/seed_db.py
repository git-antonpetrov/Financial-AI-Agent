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
from src.simulations.db.invest.db.client import init_db, get_async_session_maker
from src.simulations.db.invest.db.models import InvestmentStrategy, SavingsAccount, Deposit, BrokerAccount

AsyncSessionLocal = get_async_session_maker()

def random_date_past_months(months=6):
    start_date = datetime.now() - timedelta(days=months*30)
    end_date = datetime.now()
    delta = end_date - start_date
    random_days = random.randrange(delta.days)
    return start_date + timedelta(days=random_days)

def random_next_payment_date():
    start_date = datetime.now()
    end_date = start_date + timedelta(days=30)
    delta = end_date - start_date
    random_days = random.randrange(delta.days)
    return start_date + timedelta(days=random_days)

async def create_database_if_not_exists():
    DATABASE_URL = os.getenv(
        "INVEST_DATABASE_URL", 
        "postgresql+asyncpg://agent_user:agent_password@localhost:15432/invest_db"
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
        print("Создаем инвестиционные стратегии (20 шт)...")
        strategies = []
        templates = [
            ("Ультра-консервативная", 8.0, "Низкий", "ОФЗ, Гособлигации"),
            ("Сбалансированный портфель", 14.5, "Средний", "ОФЗ, Корпоративные облигации, Акции голубых фишек"),
            ("Дивидендная зарплата", 16.0, "Средний", "Дивидендные акции РФ, Облигации"),
            ("Агрессивный рост", 25.0, "Высокий", "Акции роста, IPO, Фьючерсы"),
            ("Технологический прорыв", 30.0, "Высокий", "Акции IT сектора, Опционы"),
            ("Антикризисная", 12.0, "Низкий", "Золото, ОФЗ, Валютные активы"),
            ("Долговой рынок", 11.5, "Низкий", "ВДО, Корпоративные облигации"),
            ("Крипто-тренд", 80.0, "Высокий", "Криптовалюта, Фьючерсы на биткоин"),
            ("Сырьевой цикл", 20.0, "Средний", "Нефть, Газ, Металлурги"),
            ("Финансовый сектор", 18.0, "Средний", "Акции банков и финтех-компаний"),
            ("Ритейл и потребление", 15.0, "Средний", "Акции ритейла, FMCG"),
            ("Инфраструктура", 13.0, "Низкий", "Транспорт, Энергетика"),
            ("Защитная гавань", 9.0, "Низкий", "Золото, Фонды денежного рынка"),
            ("Высокодоходные облигации", 22.0, "Средний", "Мусорные облигации (ВДО)"),
            ("Моментум-трейдинг", 40.0, "Высокий", "Торговля по тренду, Шорт, Маржиналка"),
            ("Глобальный сдвиг", 28.0, "Высокий", "Валютные пары, Фьючерсы на индексы"),
            ("Пассивный доход", 10.0, "Низкий", "ПИФы недвижимости, ОФЗ"),
            ("Венчур и стартапы", 60.0, "Высокий", "Pre-IPO, Краудлендинг"),
            ("Эко-тренд (ESG)", 14.0, "Средний", "Зеленая энергетика"),
            ("Спекулятивная", 50.0, "Высокий", "Интрадей трейдинг, Опционы")
        ]
        
        for name, expected_yield, risk, instruments in templates:
            fee = random.uniform(5.0, 25.0) # Комиссия от 5% до 25%
            strat = InvestmentStrategy(
                name=name,
                expected_yield=expected_yield,
                risk_level=risk,
                instruments=instruments,
                commission_fee=round(fee, 2)
            )
            strategies.append(strat)
            session.add(strat)
            
        await session.flush()
        
        client_ids = [
            "11111111-1111-1111-1111-111111111111", # Иванов
            "22222222-2222-2222-2222-222222222222", # Петров
            "33333333-3333-3333-3333-333333333333"  # Васильков
        ]
        
        print("Создаем счета клиентов...")
        for client_id in client_ids:
            # Накопительные счета
            for _ in range(random.randint(0, 2)):
                sa = SavingsAccount(
                    client_id=client_id,
                    account_number=str(random.randint(40817810000000000000, 40817810099999999999)),
                    balance=random.uniform(5000, 500000),
                    interest_rate=random.uniform(7.0, 15.0),
                    next_payment_date=random_next_payment_date(),
                    next_payment_amount=random.uniform(50, 5000),
                    opened_at=random_date_past_months(12)
                )
                session.add(sa)
                
            # Вклады
            for _ in range(random.randint(0, 2)):
                dep = Deposit(
                    client_id=client_id,
                    account_number=str(random.randint(42301810000000000000, 42301810099999999999)),
                    balance=random.uniform(50000, 2000000),
                    interest_rate=random.uniform(12.0, 20.0),
                    term_months=random.choice([3, 6, 12, 36]),
                    next_payment_date=random_next_payment_date(),
                    next_payment_amount=random.uniform(500, 20000),
                    opened_at=random_date_past_months(6)
                )
                session.add(dep)
                
            # Брокерские счета
            for _ in range(random.randint(1, 2)):
                strat = random.choice(strategies) if random.random() > 0.5 else None
                strat_id = strat.id if strat else None
                
                # Если подписан на стратегию, случайным образом генерируем доход
                income = random.uniform(-5000, 20000) if strat_id else 0.0
                next_comm = random_next_payment_date() if strat_id else None
                
                brk = BrokerAccount(
                    client_id=client_id,
                    account_number=str(random.randint(30601810000000000000, 30601810099999999999)),
                    balance=random.uniform(1000, 100000), # свободный кэш
                    monthly_income=income,
                    strategy_id=strat_id,
                    next_commission_date=next_comm,
                    opened_at=random_date_past_months(24)
                )
                session.add(brk)

        await session.commit()
        print("База инвестиций успешно заполнена тестовыми данными!")

async def main():
    await create_database_if_not_exists()
    await seed_data()

if __name__ == "__main__":
    asyncio.run(main())
