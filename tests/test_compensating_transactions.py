import asyncio
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from fastapi import HTTPException

from src.simulations.api.routers.invest import (
    open_deposit,
    open_savings,
    open_broker_account,
    close_deposit,
    close_savings,
    OpenProductRequest,
    CloseProductRequest,
)
from src.simulations.api.routers.ruble import (
    fund_wallet,
    withdraw_wallet,
    FundWithdrawRequest,
)
from src.simulations.db.bank.db.models import Account, Transaction
from src.simulations.db.digital_ruble.db.models import Wallet
from src.simulations.db.invest.db.models import Deposit, SavingsAccount, BrokerAccount


def make_async_cm(session):
    if not hasattr(session, "add") or isinstance(session.add, AsyncMock):
        session.add = MagicMock()
    if hasattr(session, "scalar") and isinstance(session.scalar, AsyncMock):
        if isinstance(session.scalar.return_value, AsyncMock):
            session.scalar.return_value = None
    cm = MagicMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = None
    return cm


# ============================================================================
# 1. ТЕСТЫ КОМПЕНСАЦИЙ В INVEST ROUTER (ОТКРЫТИЕ ПРОДУКТОВ)
# ============================================================================

def test_open_deposit_compensates_on_invest_db_failure(monkeypatch):
    """
    При сбое создания вклада в invest_db списанные с банковского счета
    деньги должны автоматически вернуться через компенсационную транзакцию.
    """
    async def _test():
        bank_acc = Account(id="acc_bank_dep", balance=Decimal("50000.00"), client_id="c_1")
        added_bank_entities = []

        bank_session = AsyncMock()
        async def bank_scalar(stmt):
            return bank_acc

        bank_session.scalar = AsyncMock(side_effect=bank_scalar)
        bank_session.add = MagicMock(side_effect=lambda entity: added_bank_entities.append(entity))
        bank_session.commit = AsyncMock()

        invest_session = AsyncMock()
        # Имитируем сбой коммита в базе инвестиций
        invest_session.commit = AsyncMock(side_effect=RuntimeError("Database connection lost in invest_db"))

        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: make_async_cm(bank_session))
        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: make_async_cm(invest_session))

        req = OpenProductRequest(
            client_id="c_1",
            from_bank_account_id="acc_bank_dep",
            initial_amount=Decimal("15000.00"),
            term_months=12
        )

        with pytest.raises(HTTPException) as exc_info:
            await open_deposit(req)

        assert exc_info.value.status_code == 500
        assert "Ошибка создания вклада. Средства компенсированы на банковский счет" in str(exc_info.value.detail)

        # Баланс банковского счета должен полностью восстановиться
        assert bank_acc.balance == Decimal("50000.00")

        # Проверяем, что были добавлены 2 транзакции: списание и компенсация
        assert len(added_bank_entities) == 2
        expense_tx = added_bank_entities[0]
        assert expense_tx.operation_type == "transfer_out_invest"
        assert expense_tx.amount == Decimal("15000.00")

        comp_tx = added_bank_entities[1]
        assert comp_tx.operation_type == "compensation_invest_deposit"
        assert comp_tx.amount == Decimal("15000.00")
        assert comp_tx.category == "income"

    asyncio.run(_test())


def test_open_savings_compensates_on_invest_db_failure(monkeypatch):
    """
    При сбое создания накопительного счета списанные средства возвращаются на банковский счет.
    """
    async def _test():
        bank_acc = Account(id="acc_bank_sav", balance=Decimal("30000.00"), client_id="c_2")
        added_bank_entities = []

        bank_session = AsyncMock()
        bank_session.scalar = AsyncMock(return_value=bank_acc)
        bank_session.add = MagicMock(side_effect=lambda entity: added_bank_entities.append(entity))
        bank_session.commit = AsyncMock()

        invest_session = AsyncMock()
        invest_session.commit = AsyncMock(side_effect=RuntimeError("Integrity error in savings_db"))

        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: make_async_cm(bank_session))
        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: make_async_cm(invest_session))

        req = OpenProductRequest(
            client_id="c_2",
            from_bank_account_id="acc_bank_sav",
            initial_amount=Decimal("10000.00")
        )

        with pytest.raises(HTTPException) as exc_info:
            await open_savings(req)

        assert exc_info.value.status_code == 500
        assert "Ошибка создания накопительного счета. Средства компенсированы на банковский счет" in str(exc_info.value.detail)
        assert bank_acc.balance == Decimal("30000.00")

        assert len(added_bank_entities) == 2
        comp_tx = added_bank_entities[1]
        assert comp_tx.operation_type == "compensation_invest_savings"
        assert comp_tx.amount == Decimal("10000.00")

    asyncio.run(_test())


def test_open_broker_account_compensates_on_invest_db_failure(monkeypatch):
    """
    При сбое создания брокерского счета списанные средства возвращаются на банковский счет.
    """
    async def _test():
        bank_acc = Account(id="acc_bank_brk", balance=Decimal("20000.00"), client_id="c_3")
        added_bank_entities = []

        bank_session = AsyncMock()
        bank_session.scalar = AsyncMock(return_value=bank_acc)
        bank_session.add = MagicMock(side_effect=lambda entity: added_bank_entities.append(entity))
        bank_session.commit = AsyncMock()

        invest_session = AsyncMock()
        invest_session.commit = AsyncMock(side_effect=RuntimeError("Deadlock in broker creation"))

        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: make_async_cm(bank_session))
        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: make_async_cm(invest_session))

        req = OpenProductRequest(
            client_id="c_3",
            from_bank_account_id="acc_bank_brk",
            initial_amount=Decimal("5000.00")
        )

        with pytest.raises(HTTPException) as exc_info:
            await open_broker_account(req)

        assert exc_info.value.status_code == 500
        assert "Ошибка создания брокерского счета. Средства компенсированы на банковский счет" in str(exc_info.value.detail)
        assert bank_acc.balance == Decimal("20000.00")

        assert len(added_bank_entities) == 2
        comp_tx = added_bank_entities[1]
        assert comp_tx.operation_type == "compensation_invest_broker"
        assert comp_tx.amount == Decimal("5000.00")

    asyncio.run(_test())


# ============================================================================
# 2. ТЕСТЫ КОМПЕНСАЦИЙ В INVEST ROUTER (ЗАКРЫТИЕ ПРОДУКТОВ)
# ============================================================================

def test_close_deposit_compensates_when_bank_fails(monkeypatch):
    """
    При сбое зачисления закрытого вклада на банковский счет (например, несуществующий счет 404),
    статус вклада и его баланс должны быть восстановлены.
    """
    async def _test():
        dep = Deposit(id="dep_123", balance=Decimal("25000.00"), status="active", client_id="c_1")

        invest_session = AsyncMock()
        invest_session.scalar = AsyncMock(return_value=dep)
        invest_session.commit = AsyncMock()

        bank_session = AsyncMock()
        # Банковский счет не найден (404)
        bank_session.scalar = AsyncMock(return_value=None)

        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: make_async_cm(invest_session))
        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: make_async_cm(bank_session))

        req = CloseProductRequest(to_bank_account_id="non_existent_bank_acc")

        with pytest.raises(HTTPException) as exc_info:
            await close_deposit("dep_123", req)

        assert exc_info.value.status_code == 404

        # Баланс и статус вклада восстановлены компенсацией
        assert dep.status == "active"
        assert dep.balance == Decimal("25000.00")

    asyncio.run(_test())


def test_close_savings_compensates_when_bank_fails(monkeypatch):
    """
    При сбое зачисления копилки в банк накопительный счет восстанавливается.
    """
    async def _test():
        sav = SavingsAccount(id="sav_123", balance=Decimal("12000.00"), status="active", client_id="c_1")

        invest_session = AsyncMock()
        invest_session.scalar = AsyncMock(return_value=sav)
        invest_session.commit = AsyncMock()

        bank_session = AsyncMock()
        bank_session.scalar = AsyncMock(side_effect=RuntimeError("Bank service connection failure"))

        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: make_async_cm(invest_session))
        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: make_async_cm(bank_session))

        req = CloseProductRequest(to_bank_account_id="bank_acc_1")

        with pytest.raises(HTTPException) as exc_info:
            await close_savings("sav_123", req)

        assert exc_info.value.status_code == 500
        assert "Ошибка зачисления средств в банк. Копилка восстановлена" in str(exc_info.value.detail)

        assert sav.status == "active"
        assert sav.balance == Decimal("12000.00")

    asyncio.run(_test())


# ============================================================================
# 3. ТЕСТЫ КОМПЕНСАЦИЙ В RUBLE ROUTER (ПОПОЛНЕНИЕ И ВЫВОД)
# ============================================================================

def test_fund_wallet_compensates_on_ruble_failure(monkeypatch):
    """
    При пополнении кошелька цифрового рубля: если списание из банка прошло,
    но пополнение кошелька упало (кошелек не найден 404 или сбой базы),
    средства должны вернуться на банковский счет с транзакцией compensation_ruble_fund.
    """
    async def _test():
        bank_acc = Account(id="bank_fund_acc", balance=Decimal("8000.00"), client_id="c_1")
        added_txs = []

        bank_session = AsyncMock()
        bank_session.scalar = AsyncMock(return_value=bank_acc)
        bank_session.add = MagicMock(side_effect=lambda entity: added_txs.append(entity))
        bank_session.commit = AsyncMock()

        ruble_session = AsyncMock()
        # Кошелек не найден в базе цифрового рубля
        ruble_session.scalar = AsyncMock(return_value=None)

        monkeypatch.setattr("src.simulations.api.routers.ruble.bank_db", lambda: make_async_cm(bank_session))
        monkeypatch.setattr("src.simulations.api.routers.ruble.ruble_db", lambda: make_async_cm(ruble_session))

        req = FundWithdrawRequest(bank_account_id="bank_fund_acc", amount=Decimal("3000.00"))

        with pytest.raises(HTTPException) as exc_info:
            await fund_wallet("unknown_wallet", req)

        assert exc_info.value.status_code == 404

        # Баланс банковского счета восстановлен
        assert bank_acc.balance == Decimal("8000.00")

        # Проверяем добавленную компенсацию
        assert len(added_txs) == 2
        assert added_txs[0].operation_type == "transfer_out_ruble"
        assert added_txs[1].operation_type == "compensation_ruble_fund"
        assert added_txs[1].amount == Decimal("3000.00")
        assert added_txs[1].category == "income"

    asyncio.run(_test())


def test_withdraw_wallet_compensates_on_bank_failure(monkeypatch):
    """
    При выводе из кошелька цифрового рубля: если списание из кошелька прошло,
    но зачисление на банковский счет упало (счет не найден 404 или сбой базы),
    цифровые рубли должны вернуться в кошелек.
    """
    async def _test():
        wallet = Wallet(id="ruble_w_wd", balance=Decimal("4500.00"), client_id="c_1")

        ruble_session = AsyncMock()
        ruble_session.scalar = AsyncMock(return_value=wallet)
        ruble_session.commit = AsyncMock()

        bank_session = AsyncMock()
        # Банковский счет не найден для зачисления
        bank_session.scalar = AsyncMock(return_value=None)

        monkeypatch.setattr("src.simulations.api.routers.ruble.ruble_db", lambda: make_async_cm(ruble_session))
        monkeypatch.setattr("src.simulations.api.routers.ruble.bank_db", lambda: make_async_cm(bank_session))

        req = FundWithdrawRequest(bank_account_id="non_existent_bank_acc", amount=Decimal("1500.00"))

        with pytest.raises(HTTPException) as exc_info:
            await withdraw_wallet("ruble_w_wd", req)

        assert exc_info.value.status_code == 404

        # Баланс кошелька восстановлен компенсацией
        assert wallet.balance == Decimal("4500.00")

    asyncio.run(_test())
