import random
from datetime import timedelta, datetime, date
from dateutil.relativedelta import relativedelta
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from src.simulations.db.bank.db.client import get_async_session_maker
AsyncSessionLocal = get_async_session_maker()
from src.simulations.db.bank.db.models import Account, Transaction, AutoPayment
from src.core.utils.console_logger import log_info, log_error

async def process_auto_payments(session, current_date):
    """
    Обработка автоплатежей, у которых наступила дата платежа.
    """
    log_info("Bank EOD", f"Обработка автоплатежей на {current_date}...")
    
    result = await session.execute(
        select(AutoPayment).where(
            AutoPayment.is_active == True,
            AutoPayment.next_payment_date <= current_date
        )
    )
    autopayments = result.scalars().all()
    
    processed = 0
    for ap in autopayments:
        # Получаем счет
        account = await session.get(Account, ap.account_id)
        if not account or account.status != "active":
            continue
            
        if account.balance >= ap.amount:
            # Списываем деньги
            account.balance -= ap.amount
            
            # Создаем транзакцию
            tx = Transaction(
                account_id=account.id,
                category="expense",
                operation_type="auto_payment",
                amount=ap.amount,
                description=f"Автоплатеж: {ap.recipient}",
                status="completed",
                date=current_date,
                time=datetime.utcnow().time()
            )
            session.add(tx)
            
            # Обновляем дату следующего платежа
            if ap.schedule == "monthly":
                ap.next_payment_date = ap.next_payment_date + relativedelta(months=1)
            else: # weekly
                ap.next_payment_date = ap.next_payment_date + timedelta(days=7)
                
            processed += 1
        else:
            # Денег не хватает
            tx = Transaction(
                account_id=account.id,
                category="expense",
                operation_type="auto_payment",
                amount=ap.amount,
                description=f"Недостаточно средств. Автоплатеж: {ap.recipient}",
                status="failed",
                date=current_date,
                time=datetime.utcnow().time()
            )
            session.add(tx)
            # Сдвигаем на завтра чтобы попробовать снова
            ap.next_payment_date = ap.next_payment_date + timedelta(days=1)
            
    await session.commit()
    log_info("Bank EOD", f"Успешно обработано автоплатежей: {processed}")

async def process_tariff_fees(session, current_date):
    """
    Списание платы за обслуживание тарифа 1-го числа каждого месяца.
    """
    if current_date.day != 1:
        return
        
    log_info("Bank EOD", f"Списание абонентской платы по тарифам на {current_date}...")
    
    result = await session.execute(select(Account).options(selectinload(Account.tariff)).where(Account.status == "active"))
    accounts = result.scalars().all()
    
    processed = 0
    for acc in accounts:
        if acc.tariff.service_cost > 0:
            if acc.balance >= acc.tariff.service_cost:
                acc.balance -= acc.tariff.service_cost
                tx = Transaction(
                    account_id=acc.id,
                    category="expense",
                    operation_type="service_fee",
                    amount=acc.tariff.service_cost,
                    description=f"Плата за обслуживание тарифа '{acc.tariff.name}'",
                    status="completed",
                    date=current_date,
                    time=datetime.utcnow().time()
                )
                session.add(tx)
                processed += 1
            else:
                tx = Transaction(
                    account_id=acc.id,
                    category="expense",
                    operation_type="service_fee",
                    amount=acc.tariff.service_cost,
                    description=f"Недостаточно средств для платы за тариф '{acc.tariff.name}'",
                    status="failed",
                    date=current_date,
                    time=datetime.utcnow().time()
                )
                session.add(tx)
            
    await session.commit()
    log_info("Bank EOD", f"Списана абонентская плата по {processed} счетам")

async def process_salaries(session, current_date):
    """
    Начисление кастомной зарплаты 10-го и 25-го числа для конкретных клиентов.
    """
    if current_date.day not in [10, 25]:
        return
        
    log_info("Bank EOD", f"Генерация зарплат на {current_date}...")
    
    result = await session.execute(select(Account).where(Account.status == "active"))
    accounts = result.scalars().all()
    
    # Чтобы не начислять зп на все 3 счета одного клиента, будем вести учет, кому уже начислили
    paid_clients = set()
    processed = 0

    for acc in accounts:
        if acc.client_id in paid_clients:
            continue
            
        salary_amount = 0
        if acc.client_id == "11111111-1111-1111-1111-111111111111": # Иванов (Ваня)
            salary_amount = random.randint(75000, 150000)
        elif acc.client_id == "22222222-2222-2222-2222-222222222222": # Петров (Петя)
            salary_amount = random.randint(150000, 200000)
        elif acc.client_id == "33333333-3333-3333-3333-333333333333": # Васильков (Вася)
            salary_amount = random.randint(200000, 250000)
        else:
            continue

        acc.balance += salary_amount
        tx = Transaction(
            account_id=acc.id,
            category="income",
            operation_type="salary",
            amount=salary_amount,
            description=f"Поступление зарплаты",
            status="completed",
            date=current_date,
            time=datetime.utcnow().time()
        )
        session.add(tx)
        paid_clients.add(acc.client_id)
        processed += 1
            
    await session.commit()
    log_info("Bank EOD", f"Начислена зарплата {processed} клиентам")

async def run_bank_eod():
    """
    Запускает весь цикл EOD (End of Day) по реальному системному времени.
    """
    async with AsyncSessionLocal() as session:
        current_date = date.today()
        
        log_info("Bank EOD", f"=== НАЧАЛО EOD ДЛЯ ДАТЫ {current_date} ===")
        
        await process_auto_payments(session, current_date)
        await process_tariff_fees(session, current_date)
        await process_salaries(session, current_date)
        
        log_info("Bank EOD", f"=== EOD ЗАВЕРШЕН ===")
