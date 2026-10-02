import asyncio
import pytest
from datetime import datetime, date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from src.simulations.db.bank.db.models import Account, Transaction
from src.simulations.db.invest.db.models import Deposit
from src.simulations.workers.invest_eod.tasks import process_deposits, return_deposit_to_bank


def make_async_cm(session):
    if not hasattr(session, "add") or isinstance(session.add, AsyncMock):
        session.add = MagicMock()
    cm = MagicMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = None
    return cm


# ============================================================================
# ТЕСТЫ ВОЗВРАТА СРЕДСТВ ПРИ ЗАКРЫТИИ ВКЛАДА ПО СРОКУ В INVEST EOD
# ============================================================================

def test_process_deposits_maturity_refunds_to_bank_account(monkeypatch):
    """
    Проверяет, что при истечении срока вклада:
    1. Все средства (тело + проценты) переводятся на активный банковский счет клиента.
    2. Создается транзакция типа transfer_in_invest (income).
    3. Баланс вклада обнуляется (0.00) и статус становится 'closed'.
    """
    async def _test():
        # Вклад открыт 2026-04-01 на 6 месяцев. Срок истекает 2026-10-01.
        dep = Deposit(
            id="dep_mature_1",
            client_id="cl_mature_user",
            account_number="42301810000000000001",
            balance=Decimal("100000.00"),
            interest_rate=Decimal("12.00"),
            term_months=6,
            status="active",
            next_payment_date=datetime(2026, 10, 1),
            next_payment_amount=Decimal("1000.00"),
            opened_at=datetime(2026, 4, 1)
        )

        bank_acc = Account(
            id="bank_acc_mature",
            client_id="cl_mature_user",
            account_number="40817810000000000001",
            balance=Decimal("5000.00"),
            status="active"
        )
        added_bank_txs = []

        bank_session = AsyncMock()
        bank_session.scalar = AsyncMock(return_value=bank_acc)
        bank_session.add = MagicMock(side_effect=lambda tx: added_bank_txs.append(tx))
        bank_session.commit = AsyncMock()

        invest_session = AsyncMock()
        invest_session.commit = AsyncMock()
        mock_dep_res = MagicMock()
        mock_dep_res.scalars().all.return_value = [dep]
        invest_session.execute = AsyncMock(return_value=mock_dep_res)

        monkeypatch.setattr("src.simulations.workers.invest_eod.tasks.get_bank_session_maker", lambda: lambda: make_async_cm(bank_session))

        # Запускаем EOD на дату истечения срока
        current_date = date(2026, 10, 1)
        await process_deposits(invest_session, current_date)

        # 1. Проверяем баланс банковского счета: 5000 (исходный) + 100000 (тело) + 1000 (проценты) = 106000.00
        assert bank_acc.balance == Decimal("106000.00")

        # 2. Проверяем транзакцию в банке
        assert len(added_bank_txs) == 1
        tx = added_bank_txs[0]
        assert tx.category == "income"
        assert tx.operation_type == "transfer_in_invest"
        assert tx.amount == Decimal("101000.00")
        assert "42301810000000000001" in tx.description

        # 3. Проверяем состояние депозита
        assert dep.status == "closed"
        assert dep.balance == Decimal("0.00")

    asyncio.run(_test())


def test_process_deposits_maturity_not_reached_no_refund(monkeypatch):
    """
    Проверяет, что если срок вклада еще не истек:
    - проценты начисляются на баланс вклада;
    - вклад остается активным;
    - деньги на банковский счет не переводятся.
    """
    async def _test():
        # Вклад открыт 2026-08-01 на 12 месяцев. На дату 2026-10-01 прошло только 2 месяца.
        dep = Deposit(
            id="dep_not_mature",
            client_id="cl_active_user",
            account_number="42301810000000000002",
            balance=Decimal("50000.00"),
            interest_rate=Decimal("12.00"),
            term_months=12,
            status="active",
            next_payment_date=datetime(2026, 10, 1),
            next_payment_amount=Decimal("500.00"),
            opened_at=datetime(2026, 8, 1)
        )

        bank_session = AsyncMock()
        monkeypatch.setattr("src.simulations.workers.invest_eod.tasks.get_bank_session_maker", lambda: lambda: make_async_cm(bank_session))

        invest_session = AsyncMock()
        invest_session.commit = AsyncMock()
        mock_dep_res = MagicMock()
        mock_dep_res.scalars().all.return_value = [dep]
        invest_session.execute = AsyncMock(return_value=mock_dep_res)

        current_date = date(2026, 10, 1)
        await process_deposits(invest_session, current_date)

        # Депозит капитализировал проценты: 50 000 + 500 = 50 500
        assert dep.balance == Decimal("50500.00")
        assert dep.status == "active"

        # В банк не должно быть никаких обращений
        assert bank_session.scalar.call_count == 0

    asyncio.run(_test())


def test_process_deposits_failsafe_when_no_bank_account(monkeypatch):
    """
    Проверяет защиту от потери средств (failsafe):
    Если у клиента нет доступного банковского счета при истечении срока,
    вклад НЕ закрывается и его баланс НЕ обнуляется.
    """
    async def _test():
        dep = Deposit(
            id="dep_failsafe",
            client_id="cl_no_bank_acc",
            account_number="42301810000000000003",
            balance=Decimal("70000.00"),
            interest_rate=Decimal("12.00"),
            term_months=3,
            status="active",
            next_payment_date=datetime(2026, 10, 1),
            next_payment_amount=Decimal("700.00"),
            opened_at=datetime(2026, 7, 1)
        )

        bank_session = AsyncMock()
        # Активный банковский счет не найден
        bank_session.scalar = AsyncMock(return_value=None)

        invest_session = AsyncMock()
        invest_session.commit = AsyncMock()
        mock_dep_res = MagicMock()
        mock_dep_res.scalars().all.return_value = [dep]
        invest_session.execute = AsyncMock(return_value=mock_dep_res)

        monkeypatch.setattr("src.simulations.workers.invest_eod.tasks.get_bank_session_maker", lambda: lambda: make_async_cm(bank_session))

        current_date = date(2026, 10, 1)
        await process_deposits(invest_session, current_date)

        # Деньги НЕ пропали: баланс сохранен (с начисленными процентами 70 700), вклад остался активным
        assert dep.balance == Decimal("70700.00")
        assert dep.status == "active"

    asyncio.run(_test())
