import random
from datetime import timedelta, date, datetime
from dateutil.relativedelta import relativedelta
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from decimal import Decimal

from src.simulations.db.invest.db.client import get_async_session_maker
AsyncSessionLocal = get_async_session_maker()
from src.simulations.db.bank.db.client import get_async_session_maker as get_bank_session_maker
from src.simulations.db.bank.db.models import Account, Transaction
from src.simulations.db.invest.db.models import SavingsAccount, Deposit, BrokerAccount, InvestmentStrategy
from src.simulations.core.utils.console_logger import log_info, log_error

async def return_deposit_to_bank(client_id: str, deposit_number: str, amount: Decimal) -> bool:
    """
    Возвращает тело вклада и начисленные проценты на активный банковский счет клиента.
    Возвращает True в случае успешного зачисления, False если счет не найден или произошла ошибка.
    """
    if amount <= Decimal("0.00"):
        return True

    try:
        bank_session_maker = get_bank_session_maker()
        async with bank_session_maker() as b_session:
            acc = await b_session.scalar(
                select(Account)
                .where(Account.client_id == client_id, Account.status == "active")
                .order_by(Account.created_at)
                .with_for_update()
            )
            if not acc:
                log_error("Invest EOD", f"У клиента {client_id} не найден активный банковский счет для возврата вклада {deposit_number}")
                return False

            acc.balance = Decimal(str(acc.balance)) + amount
            tx = Transaction(
                account_id=acc.id,
                category='income',
                operation_type='transfer_in_invest',
                amount=amount,
                description=f"Возврат вклада {deposit_number} по истечении срока"
            )
            b_session.add(tx)
            await b_session.commit()
            log_info("Invest EOD", f"Средства вклада {deposit_number} ({amount} руб.) успешно возвращены на банковский счет {acc.account_number}")
            return True
    except Exception as e:
        log_error("Invest EOD", f"Ошибка при возврате вклада {deposit_number} на банковский счет: {e}")
        return False

async def process_savings_accounts(session, current_date):
    """
    Выплата процентов по накопительным счетам.
    """
    log_info("Invest EOD", f"Обработка накопительных счетов на {current_date}...")
    
    current_datetime = datetime.combine(current_date, datetime.max.time())

    # Берем только АКТИВНЫЕ счета, у которых наступила дата выплаты
    result = await session.execute(
        select(SavingsAccount).where(
            SavingsAccount.status == "active",
            SavingsAccount.next_payment_date <= current_datetime
        )
    )
    accounts = result.scalars().all()
    
    processed = 0
    for acc in accounts:
        if acc.next_payment_amount and Decimal(str(acc.next_payment_amount)) > Decimal("0.00"):
            # Выплачиваем проценты (капитализация)
            acc.balance = Decimal(str(acc.balance)) + Decimal(str(acc.next_payment_amount))
            
        # Считаем сумму следующей выплаты: баланс * (ставка / 100) / 12 месяцев
        monthly_rate = Decimal(str(acc.interest_rate)) / Decimal('100') / Decimal('12')
        acc.next_payment_amount = round(Decimal(str(acc.balance)) * monthly_rate, 2)
        
        # Сдвигаем дату на 1 месяц вперед
        acc.next_payment_date = acc.next_payment_date + relativedelta(months=1)
        processed += 1
            
    await session.commit()
    log_info("Invest EOD", f"Успешно обработано накопительных счетов: {processed}")

async def process_deposits(session, current_date):
    """
    Выплата процентов по вкладам и их закрытие по истечению срока с возвратом средств в банк.
    """
    log_info("Invest EOD", f"Обработка вкладов на {current_date}...")
    
    current_datetime = datetime.combine(current_date, datetime.max.time())

    result = await session.execute(
        select(Deposit).where(
            Deposit.status == "active",
            Deposit.next_payment_date <= current_datetime
        )
    )
    deposits = result.scalars().all()
    
    processed = 0
    closed = 0
    for dep in deposits:
        if dep.next_payment_amount and Decimal(str(dep.next_payment_amount)) > Decimal("0.00"):
            dep.balance = Decimal(str(dep.balance)) + Decimal(str(dep.next_payment_amount))
            
        monthly_rate = Decimal(str(dep.interest_rate)) / Decimal('100') / Decimal('12')
        dep.next_payment_amount = round(Decimal(str(dep.balance)) * monthly_rate, 2)
        
        dep.next_payment_date = dep.next_payment_date + relativedelta(months=1)
        processed += 1
        
        # Проверяем не истек ли срок вклада
        if dep.opened_at:
            maturity_date = dep.opened_at.date() + relativedelta(months=int(dep.term_months))
        else:
            maturity_date = current_date + relativedelta(months=1)

        if current_date >= maturity_date:
            amount_to_return = Decimal(str(dep.balance))
            refund_ok = await return_deposit_to_bank(
                client_id=dep.client_id,
                deposit_number=dep.account_number,
                amount=amount_to_return
            )
            if refund_ok:
                dep.balance = Decimal("0.00")
                dep.status = "closed"
                closed += 1
            else:
                log_error(
                    "Invest EOD",
                    f"Закрытие вклада {dep.account_number} отложено: не удалось перевести средства на банковский счет"
                )
            
    await session.commit()
    log_info("Invest EOD", f"Обработано вкладов: {processed}. Из них закрыто по сроку: {closed}")

async def process_broker_accounts(session, current_date):
    """
    Генерация рыночной доходности и списание Success Fee (комиссии) по брокерским счетам.
    """
    log_info("Invest EOD", f"Обработка брокерских счетов на {current_date}...")
    
    current_datetime = datetime.combine(current_date, datetime.max.time())

    result = await session.execute(
        select(BrokerAccount).options(selectinload(BrokerAccount.strategy)).where(
            BrokerAccount.status == "active",
            BrokerAccount.next_commission_date <= current_datetime
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
        base_monthly_yield = Decimal(str(strategy.expected_yield)) / Decimal("12")
        
        # Задаем разброс (шум) в зависимости от риска
        risk_noise = {
            "Low": Decimal("0.5"),      # +- 0.5% колебания
            "Medium": Decimal("2.0"),   # +- 2.0% колебания
            "High": Decimal("5.0")      # +- 5.0% колебания (может уйти в сильный минус)
        }
        noise_level = risk_noise.get(strategy.risk_level, Decimal("1.0"))
        noise = Decimal(str(round(random.uniform(-float(noise_level), float(noise_level)), 4)))
        actual_monthly_yield = base_monthly_yield + noise
        
        # Считаем доход за прошедший месяц
        income = round(Decimal(str(acc.balance)) * (actual_monthly_yield / Decimal("100")), 2)
        acc.monthly_income = income
        
        # Применяем доход к балансу (он может быть и отрицательным!)
        acc.balance = Decimal(str(acc.balance)) + acc.monthly_income
        
        # Если месяц закрыт в плюс, берем комиссию за успех (Success Fee)
        if acc.monthly_income > Decimal("0.00"):
            fee = round(Decimal(str(acc.monthly_income)) * (Decimal(str(strategy.commission_fee)) / Decimal('100')), 2)
            acc.balance = Decimal(str(acc.balance)) - fee
            
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
