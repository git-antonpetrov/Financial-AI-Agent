import inspect
import random
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from src.simulations.db.invest.db.client import get_async_session_maker
from src.simulations.workers.advisory_lock import acquire_advisory_lock, release_advisory_lock

AsyncSessionLocal = get_async_session_maker()
from src.simulations.core.utils.console_logger import log_error, log_info
from src.simulations.db.bank.db.client import (
    get_async_session_maker as get_bank_session_maker,
)
from src.simulations.core.bank_claims import begin_bank_movement, canonical_amount, remember_bank_movement
from src.simulations.db.bank.db.models import Account, Transaction, get_moscow_date
from src.simulations.db.invest.db.models import (
    BrokerAccount,
    Deposit,
    SavingsAccount,
)


@asynccontextmanager
async def _item_savepoint(session: AsyncSession):
    """
    Создает изолированный вложенный savepoint (session.begin_nested()) для обработки отдельного элемента.
    Предотвращает откат всей транзакции при сбое на одном элементе.
    """
    if hasattr(session, "begin_nested"):
        nested = session.begin_nested()
        if hasattr(nested, "__aenter__"):
            async with nested:
                yield
            return
        elif inspect.iscoroutine(nested):
            nested.close()
            yield
            return
    yield



async def return_deposit_to_bank(
    client_id: str,
    deposit_number: str,
    amount: Decimal,
    payout_key: str | None = None,
) -> bool:
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
                .where(
                    Account.client_id == client_id,
                    Account.status == "active",
                    (Account.currency == "RUB") | (Account.currency.is_(None)),
                )
                .order_by(Account.created_at)
                .with_for_update()
            )
            if not acc:
                log_error(
                    "Invest EOD",
                    f"У клиента {client_id} не найден активный банковский счет для возврата вклада {deposit_number}",
                )
                return False

            claim_key = payout_key or f"eod-mature:{deposit_number}"
            should_post = await begin_bank_movement(
                b_session,
                key=claim_key,
                operation_type="eod_deposit_return",
                account_id=str(acc.id),
                amount=amount,
                direction="income",
            )
            if not should_post:
                log_info(
                    "Invest EOD",
                    f"Вклад {deposit_number} уже был зачислен на счет {acc.account_number} ранее (идемпотентность)",
                )
                return True

            acc.balance = Decimal(str(acc.balance)) + amount
            tx = Transaction(
                account_id=acc.id,
                category="income",
                operation_type="transfer_in_invest",
                amount=amount,
                description=f"Возврат вклада {deposit_number} по истечении срока",
            )
            b_session.add(tx)
            remember_bank_movement(
                b_session,
                key=claim_key,
                operation_type="eod_deposit_return",
                account_id=str(acc.id),
                amount=amount,
                direction="income",
            )
            await b_session.commit()
            log_info(
                "Invest EOD",
                f"Средства вклада {deposit_number} ({amount} руб.) "
                f"успешно возвращены на банковский счет {acc.account_number}",
            )
            return True
    except Exception as e:
        log_error(
            "Invest EOD",
            f"Ошибка при возврате вклада {deposit_number} на банковский счет: {e}",
        )
        return False


async def process_savings_accounts(session: AsyncSession, current_date: date) -> None:
    """
    Выплата процентов по накопительным счетам.
    """
    log_info("Invest EOD", f"Обработка накопительных счетов на {current_date}...")

    current_datetime = datetime.combine(current_date, datetime.max.time())

    # Оставляет только активные счета с наступившей датой выплаты и блокирует строку.
    result = await session.execute(
        select(SavingsAccount)
        .where(
            SavingsAccount.status == "active",
            SavingsAccount.next_payment_date <= current_datetime,
        )
        .with_for_update()
    )
    accounts = result.scalars().all()

    processed = 0
    for acc in accounts:
        try:
            async with _item_savepoint(session):
                if acc.status != "active" or acc.payout_key:
                    continue
                if acc.next_payment_amount and Decimal(str(acc.next_payment_amount)) > Decimal("0.00"):
                    # Выплачивает проценты (капитализация)
                    acc.balance = Decimal(str(acc.balance)) + Decimal(str(acc.next_payment_amount))

                # Считает сумму следующей выплаты: баланс * (ставка / 100) / 12 месяцев
                monthly_rate = Decimal(str(acc.interest_rate)) / Decimal(100) / Decimal(12)
                acc.next_payment_amount = (Decimal(str(acc.balance)) * monthly_rate).quantize(Decimal("0.01"))

                # Сдвигает дату на 1 месяц вперед
                acc.next_payment_date = acc.next_payment_date + relativedelta(months=1)
                processed += 1
        except Exception as e:
            log_error("Invest EOD", f"Ошибка при обработке накопительного счета {acc.id}: {e}")
            continue

    await session.commit()
    log_info("Invest EOD", f"Успешно обработано накопительных счетов: {processed}")


async def process_deposits(session: AsyncSession, current_date: date) -> None:
    """
    Выплата процентов по вкладам и их закрытие по истечению срока с возвратом средств в банк.
    """
    log_info("Invest EOD", f"Обработка вкладов на {current_date}...")

    current_datetime = datetime.combine(current_date, datetime.max.time())

    result = await session.execute(
        select(Deposit)
        .where(Deposit.status == "active", Deposit.next_payment_date <= current_datetime)
        .with_for_update()
    )
    deposits = result.scalars().all()

    processed = 0
    closed = 0
    for dep in deposits:
        try:
            async with _item_savepoint(session):
                if dep.status != "active":
                    continue
                if dep.payout_key and not str(dep.payout_key).startswith("eod-mature:"):
                    continue
                payout_already_scheduled = bool(dep.payout_key)
                if (
                    not payout_already_scheduled
                    and dep.next_payment_amount
                    and Decimal(str(dep.next_payment_amount)) > Decimal("0.00")
                ):
                    dep.balance = Decimal(str(dep.balance)) + Decimal(str(dep.next_payment_amount))

                if not payout_already_scheduled:
                    monthly_rate = Decimal(str(dep.interest_rate)) / Decimal(100) / Decimal(12)
                    dep.next_payment_amount = (Decimal(str(dep.balance)) * monthly_rate).quantize(Decimal("0.01"))

                # Сверяет дату открытия и срок с текущей датой закрытия.
                if dep.opened_at:
                    maturity_date = dep.opened_at.date() + relativedelta(months=int(dep.term_months))
                else:
                    maturity_date = current_date + relativedelta(months=1)

                if current_date >= maturity_date:
                    client_id = str(dep.client_id) if dep.client_id else ""
                    deposit_number = str(dep.account_number) if dep.account_number else str(dep.id)
                    payout_key = dep.payout_key or f"eod-mature:{dep.id}"
                    if not dep.payout_key:
                        dep.payout_key = payout_key
                        dep.payout_amount = canonical_amount(Decimal(str(dep.balance)))
                    amount_to_return = Decimal(str(dep.payout_amount or dep.balance))
                    refund_ok = await return_deposit_to_bank(
                        client_id=client_id,
                        deposit_number=deposit_number,
                        amount=amount_to_return,
                        payout_key=payout_key,
                    )
                    if refund_ok:
                        dep.balance = Decimal("0.00")
                        dep.status = "closed"
                        closed += 1
                    else:
                        log_error(
                            "Invest EOD",
                            f"Закрытие вклада {dep.account_number} отложено: ошибка перевода на банковский счет",
                        )
                else:
                    dep.next_payment_date = dep.next_payment_date + relativedelta(months=1)
                processed += 1
        except Exception as e:
            log_error("Invest EOD", f"Ошибка при обработке депозита {dep.id}: {e}")
            continue

    await session.commit()
    log_info(
        "Invest EOD",
        f"Обработано вкладов: {processed}. Из них закрыто по сроку: {closed}",
    )


async def process_broker_accounts(session: AsyncSession, current_date: date) -> None:
    """
    Генерация рыночной доходности и списание Success Fee (комиссии) по брокерским счетам.
    """
    log_info("Invest EOD", f"Обработка брокерских счетов на {current_date}...")

    current_datetime = datetime.combine(current_date, datetime.max.time())

    result = await session.execute(
        select(BrokerAccount)
        .options(selectinload(BrokerAccount.strategy))
        .where(
            BrokerAccount.status == "active",
            BrokerAccount.next_commission_date <= current_datetime,
        )
        .with_for_update()
    )
    accounts = result.scalars().all()

    processed = 0
    for acc in accounts:
        try:
            async with _item_savepoint(session):
                if acc.status != "active":
                    continue
                if not acc.strategy:
                    # Если нет стратегии, просто сдвигает дату, деньги лежат мертвым грузом
                    acc.next_commission_date = acc.next_commission_date + relativedelta(months=1)
                    continue

                # Генерация рыночного шума на основе риск-профиля
                strategy = acc.strategy
                base_monthly_yield = Decimal(str(strategy.expected_yield)) / Decimal(12)

                # Задаёт разброс (шум) в зависимости от риска
                risk_noise = {
                    "Low": Decimal("0.5"),  # +- 0.5% колебания
                    "Medium": Decimal("2.0"),  # +- 2.0% колебания
                    "High": Decimal("5.0"),  # +- 5.0% колебания (может уйти в сильный минус)
                }
                noise_level = risk_noise.get(strategy.risk_level, Decimal("1.0"))
                noise = Decimal(str(round(random.uniform(-float(noise_level), float(noise_level)), 4)))
                actual_monthly_yield = base_monthly_yield + noise

                # Считает доход за прошедший месяц
                income = (Decimal(str(acc.balance)) * (actual_monthly_yield / Decimal(100))).quantize(Decimal("0.01"))
                acc.monthly_income = income

                # Применяет доход к балансу (он может быть и отрицательным!)
                acc.balance = Decimal(str(acc.balance)) + acc.monthly_income

                # Если месяц закрыт в плюс, берёт комиссию за успех (Success Fee)
                if acc.monthly_income > Decimal("0.00"):
                    rate = Decimal(str(strategy.commission_fee)) / Decimal(100)
                    fee = (Decimal(str(acc.monthly_income)) * rate).quantize(Decimal("0.01"))
                    acc.balance = Decimal(str(acc.balance)) - fee

                # Сдвигает дату следующего расчета
                acc.next_commission_date = acc.next_commission_date + relativedelta(months=1)
                processed += 1
        except Exception as e:
            log_error("Invest EOD", f"Ошибка при обработке брокерского счета {acc.id}: {e}")
            continue

    await session.commit()
    log_info("Invest EOD", f"Обработано брокерских счетов (по стратегиям): {processed}")


async def run_invest_eod() -> None:
    """
    Запускает весь цикл EOD для инвестиций по реальному системному времени.
    """
    async with AsyncSessionLocal() as session:
        lock_conn, locked = await acquire_advisory_lock(session, 84920482)
        if not locked:
            log_info("Invest EOD", "Параллельный процесс Invest EOD уже выполняется. Пропуск итерации.")
            return

        try:
            current_date = get_moscow_date()

            log_info("Invest EOD", f"=== НАЧАЛО EOD ИНВЕСТИЦИЙ ДЛЯ ДАТЫ {current_date} ===")

            await process_savings_accounts(session, current_date)
            await process_deposits(session, current_date)
            await process_broker_accounts(session, current_date)

            log_info("Invest EOD", "=== EOD ИНВЕСТИЦИЙ ЗАВЕРШЕН ===")
        finally:
            await release_advisory_lock(lock_conn, session, 84920482)
