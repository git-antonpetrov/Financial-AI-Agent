import asyncio
import pytest
from datetime import datetime, date, timedelta
from dateutil.relativedelta import relativedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from src.simulations.api.routers.invest import (
    open_deposit,
    open_savings,
    open_broker_account,
    subscribe_strategy,
    OpenProductRequest,
    StrategySubscription,
)
from src.simulations.db.bank.db.models import Account
from src.simulations.db.invest.db.models import Deposit, SavingsAccount, BrokerAccount
from src.simulations.workers.invest_eod.tasks import process_deposits, process_savings_accounts


def make_async_cm(session):
    if not hasattr(session, "add") or isinstance(session.add, AsyncMock):
        session.add = MagicMock()
    cm = MagicMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = None
    return cm


# ============================================================================
# ТЕСТЫ ИНИЦИАЛИЗАЦИИ NEXT_PAYMENT_DATE И NEXT_COMMISSION_DATE
# ============================================================================

def test_open_deposit_initializes_next_payment_date_and_amount(monkeypatch):
    """
    Проверяет, что при открытии вклада:
    - next_payment_date устанавливается на 1 месяц вперед;
    - next_payment_amount рассчитывается по формуле (баланс * ставка / 12).
    """
    async def _test():
        bank_acc = Account(id="bank_acc_1", balance=Decimal("100000.00"), client_id="cl_1")
        created_deposits = []

        bank_session = AsyncMock()
        bank_session.scalar = AsyncMock(return_value=bank_acc)
        bank_session.commit = AsyncMock()

        invest_session = AsyncMock()
        invest_session.add = MagicMock(side_effect=lambda dep: created_deposits.append(dep))
        invest_session.commit = AsyncMock()
        invest_session.refresh = AsyncMock()

        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: make_async_cm(bank_session))
        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: make_async_cm(invest_session))

        now_before = datetime.now()
        req = OpenProductRequest(
            client_id="cl_1",
            from_bank_account_id="bank_acc_1",
            initial_amount=Decimal("60000.00"),
            term_months=6
        )
        res = await open_deposit(req)

        assert len(created_deposits) == 1
        dep = created_deposits[0]

        # 1. Проверяем next_payment_date
        assert dep.next_payment_date is not None
        assert isinstance(dep.next_payment_date, datetime)
        # Должна быть приблизительно на 1 месяц вперед (+28..31 дней)
        delta_days = (dep.next_payment_date - now_before).days
        assert 28 <= delta_days <= 32

        # 2. Проверяем next_payment_amount
        # 60 000 * 15% / 12 = 750.00
        expected_amount = round(Decimal("60000.00") * (Decimal("15.00") / Decimal("100") / Decimal("12")), 2)
        assert isinstance(dep.next_payment_amount, Decimal)
        assert dep.next_payment_amount == expected_amount
        assert dep.next_payment_amount == Decimal("750.00")

        # 3. Проверяем, что в ответе API есть даты и суммы
        assert "next_payment_date" in res
        assert "next_payment_amount" in res

    asyncio.run(_test())


def test_open_savings_initializes_next_payment_date_and_amount(monkeypatch):
    """
    Проверяет, что при открытии накопительного счета:
    - next_payment_date устанавливается на 1 месяц вперед;
    - next_payment_amount рассчитывается по формуле (баланс * 10% / 12).
    """
    async def _test():
        bank_acc = Account(id="bank_acc_2", balance=Decimal("50000.00"), client_id="cl_2")
        created_savings = []

        bank_session = AsyncMock()
        bank_session.scalar = AsyncMock(return_value=bank_acc)
        bank_session.commit = AsyncMock()

        invest_session = AsyncMock()
        invest_session.add = MagicMock(side_effect=lambda sav: created_savings.append(sav))
        invest_session.commit = AsyncMock()
        invest_session.refresh = AsyncMock()

        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: make_async_cm(bank_session))
        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: make_async_cm(invest_session))

        now_before = datetime.now()
        req = OpenProductRequest(
            client_id="cl_2",
            from_bank_account_id="bank_acc_2",
            initial_amount=Decimal("12000.00")
        )
        res = await open_savings(req)

        assert len(created_savings) == 1
        sav = created_savings[0]

        # 1. Проверяем next_payment_date
        assert sav.next_payment_date is not None
        assert isinstance(sav.next_payment_date, datetime)
        delta_days = (sav.next_payment_date - now_before).days
        assert 28 <= delta_days <= 32

        # 2. Проверяем next_payment_amount
        # 12 000 * 10% / 12 = 100.00
        expected_amount = round(Decimal("12000.00") * (Decimal("10.00") / Decimal("100") / Decimal("12")), 2)
        assert isinstance(sav.next_payment_amount, Decimal)
        assert sav.next_payment_amount == expected_amount
        assert sav.next_payment_amount == Decimal("100.00")

        # 3. Проверяем наличие в ответе
        assert "next_payment_date" in res
        assert "next_payment_amount" in res

    asyncio.run(_test())


def test_open_broker_account_initializes_next_commission_date(monkeypatch):
    """
    Проверяет, что при открытии брокерского счета next_commission_date
    инициализируется датой на 1 месяц вперед.
    """
    async def _test():
        bank_acc = Account(id="bank_acc_3", balance=Decimal("30000.00"), client_id="cl_3")
        created_brokers = []

        bank_session = AsyncMock()
        bank_session.scalar = AsyncMock(return_value=bank_acc)
        bank_session.commit = AsyncMock()

        invest_session = AsyncMock()
        invest_session.add = MagicMock(side_effect=lambda brk: created_brokers.append(brk))
        invest_session.commit = AsyncMock()
        invest_session.refresh = AsyncMock()

        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: make_async_cm(bank_session))
        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: make_async_cm(invest_session))

        now_before = datetime.now()
        req = OpenProductRequest(
            client_id="cl_3",
            from_bank_account_id="bank_acc_3",
            initial_amount=Decimal("15000.00")
        )
        res = await open_broker_account(req)

        assert len(created_brokers) == 1
        brk = created_brokers[0]

        assert brk.next_commission_date is not None
        assert isinstance(brk.next_commission_date, datetime)
        delta_days = (brk.next_commission_date - now_before).days
        assert 28 <= delta_days <= 32
        assert "next_commission_date" in res

    asyncio.run(_test())


def test_subscribe_strategy_initializes_commission_date_if_none(monkeypatch):
    """
    Проверяет, что если брокерский счет имел next_commission_date=None,
    при подключении стратегии дата комиссии выставляется на месяц вперед.
    """
    async def _test():
        brk = BrokerAccount(id="brk_no_comm", balance=Decimal("10000.00"), next_commission_date=None)

        invest_session = AsyncMock()
        invest_session.scalar = AsyncMock(return_value=brk)
        invest_session.commit = AsyncMock()

        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: make_async_cm(invest_session))

        now_before = datetime.now()
        await subscribe_strategy("brk_no_comm", StrategySubscription(strategy_id="strat_123"))

        assert brk.strategy_id == "strat_123"
        assert brk.next_commission_date is not None
        assert isinstance(brk.next_commission_date, datetime)
        delta_days = (brk.next_commission_date - now_before).days
        assert 28 <= delta_days <= 32

    asyncio.run(_test())


def test_eod_worker_processes_newly_opened_deposit(monkeypatch):
    """
    Интеграционная проверка: проверяет, что когда наступает next_payment_date,
    EOD-воркер отбирает созданный вклад и начисляет проценты.
    """
    async def _test():
        # Допустим, вклад был открыт месяц назад
        payment_date = datetime(2026, 10, 1)
        dep = Deposit(
            id="dep_eod_test",
            balance=Decimal("60000.00"),
            interest_rate=Decimal("15.00"),
            term_months=12,
            status="active",
            next_payment_date=payment_date,
            next_payment_amount=Decimal("750.00"),
            opened_at=datetime(2026, 9, 1)
        )

        session = AsyncMock()
        session.commit = AsyncMock()
        mock_res = MagicMock()
        mock_res.scalars().all.return_value = [dep]
        session.execute = AsyncMock(return_value=mock_res)

        current_date = date(2026, 10, 2)
        await process_deposits(session, current_date)

        # Баланс увеличился на сумму выплаты (60 000 + 750 = 60 750)
        assert dep.balance == Decimal("60750.00")
        # Следующая дата выплаты сдвинулась на месяц
        assert dep.next_payment_date.date() == date(2026, 11, 1)

    asyncio.run(_test())
