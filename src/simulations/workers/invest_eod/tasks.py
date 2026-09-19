import random
from datetime import timedelta, date
from dateutil.relativedelta import relativedelta
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from src.simulations.db.invest.db.client import get_async_session_maker
AsyncSessionLocal = get_async_session_maker()
from src.simulations.db.invest.db.models import SavingsAccount, Deposit, BrokerAccount, InvestmentStrategy
from src.core.utils.console_logger import log_info, log_error

async def process_savings_accounts(session, current_date):
    """
    Выплата процентов по накопительным счетам.
    """
    log_info("Invest EOD", f"Обработка накопительных счетов на {current_date}...")
    
    # Берем только АКТИВНЫЕ счета, у которых наступила дата выплаты
    result = await session.execute(
        select(SavingsAccount).where(
            SavingsAccount.status == "active",
            SavingsAccount.next_payment_date <= current_date
        )
    )
    accounts = result.scalars().all()
    
    processed = 0
    for acc in accounts:
        if acc.next_payment_amount > 0:
            # Выплачиваем проценты (капитализация)
            acc.balance += acc.next_payment_amount
            
        # Считаем сумму следующей выплаты: баланс * (ставка / 100) / 12 месяцев
        monthly_rate = (acc.interest_rate / 100) / 12
        acc.next_payment_amount = round(acc.balance * monthly_rate, 2)
        
        # Сдвигаем дату на 1 месяц вперед
        acc.next_payment_date = acc.next_payment_date + relativedelta(months=1)
        processed += 1
            
    await session.commit()
    log_info("Invest EOD", f"Успешно обработано накопительных счетов: {processed}")

async def process_deposits(session, current_date):
    """
    Выплата процентов по вкладам и их закрытие по истечению срока.
    """
    log_info("Invest EOD", f"Обработка вкладов на {current_date}...")
    
    result = await session.execute(
        select(Deposit).where(
            Deposit.status == "active",
            Deposit.next_payment_date <= current_date
        )
    )
    deposits = result.scalars().all()
    
    processed = 0
    closed = 0
    for dep in deposits:
        if dep.next_payment_amount > 0:
            dep.balance += dep.next_payment_amount
            
        monthly_rate = (dep.interest_rate / 100) / 12
        dep.next_payment_amount = round(dep.balance * monthly_rate, 2)
        
        dep.next_payment_date = dep.next_payment_date + relativedelta(months=1)
        processed += 1
        
        # Проверяем не истек ли срок вклада
        maturity_date = dep.opened_at.date() + relativedelta(months=int(dep.term_months))
        if current_date >= maturity_date:
            dep.status = "closed"
            closed += 1
            
    await session.commit()
    log_info("Invest EOD", f"Обработано вкладов: {processed}. Из них закрыто по сроку: {closed}")

async def process_broker_accounts(session, current_date):
    """
    Генерация рыночной доходности и списание Success Fee (комиссии) по брокерским счетам.
    """
    log_info("Invest EOD", f"Обработка брокерских счетов на {current_date}...")
    
    result = await session.execute(
        select(BrokerAccount).options(selectinload(BrokerAccount.strategy)).where(
            BrokerAccount.status == "active",
            BrokerAccount.next_commission_date <= current_date
        )
    )
    accounts = result.scalars().all()
    
    processed = 0
    for acc in accounts:
        if not acc.strategy:
            # Если нет стратегии, просто сдвигаем дату, деньги лежат мертвым грузом
            acc.next_commission_date = acc.next_commission_date + relativedelta(months=1)
            continue
            
        # Генерация рыночного шума на основе риск-профиля
        strategy = acc.strategy
        base_monthly_yield = float(strategy.expected_yield) / 12.0
        
        # Задаем разброс (шум) в зависимости от риска
        risk_noise = {
            "Low": 0.5,      # +- 0.5% колебания
            "Medium": 2.0,   # +- 2.0% колебания
            "High": 5.0      # +- 5.0% колебания (может уйти в сильный минус)
        }
        noise_level = risk_noise.get(strategy.risk_level, 1.0)
        actual_monthly_yield = base_monthly_yield + random.uniform(-noise_level, noise_level)
        
        # Считаем доход за прошедший месяц
        income = round(float(acc.balance) * (actual_monthly_yield / 100), 2)
        acc.monthly_income = income
        
        # Применяем доход к балансу (он может быть и отрицательным!)
        acc.balance += acc.monthly_income
        
        # Если месяц закрыт в плюс, берем комиссию за успех (Success Fee)
        if acc.monthly_income > 0:
            fee = round(float(acc.monthly_income) * (float(strategy.commission_fee) / 100), 2)
            acc.balance -= fee
            
        # Сдвигаем дату следующего расчета
        acc.next_commission_date = acc.next_commission_date + relativedelta(months=1)
        processed += 1
            
    await session.commit()
    log_info("Invest EOD", f"Обработано брокерских счетов (по стратегиям): {processed}")

async def run_invest_eod():
    """
    Запускает весь цикл EOD для инвестиций по реальному системному времени.
    """
    async with AsyncSessionLocal() as session:
        current_date = date.today()
        
        log_info("Invest EOD", f"=== НАЧАЛО EOD ИНВЕСТИЦИЙ ДЛЯ ДАТЫ {current_date} ===")
        
        await process_savings_accounts(session, current_date)
        await process_deposits(session, current_date)
        await process_broker_accounts(session, current_date)
        
        log_info("Invest EOD", f"=== EOD ИНВЕСТИЦИЙ ЗАВЕРШЕН ===")
