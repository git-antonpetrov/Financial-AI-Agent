import asyncio
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from fastapi import HTTPException

from src.simulations.api.routers.bank import (
    transfer_money,
    close_account,
    TransferRequest,
)
from src.simulations.api.routers.ruble import (
    transfer_rubles,
    create_smart_contract,
    RubleTransferRequest,
    CreateContractRequest,
)
from src.simulations.db.bank.db.models import Account
from src.simulations.db.digital_ruble.db.models import Wallet


def make_async_cm(session):
    if "add" not in session.__dict__:
        session.add = MagicMock()
    cm = MagicMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = None
    return cm


# ============================================================================
# 1. ТЕСТЫ БАНКА: САМОПЕРЕВОД И ЗАКРЫТИЕ СЧЕТА С НЕНУЛЕВЫМ БАЛАНСОМ
# ============================================================================

def test_bank_transfer_rejects_self_transfer(monkeypatch):
    """
    Проверяет, что попытка перевода на тот же самый счет отклоняется с HTTP 400.
    """
    async def _test():
        acc = Account(id="acc_same", account_number="40817810000000000001", balance=Decimal("1000.00"), status="active")
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=acc)

        monkeypatch.setattr("src.simulations.api.routers.bank.get_db", lambda: make_async_cm(session))

        req = TransferRequest(
            from_account_id="acc_same",
            amount=Decimal("100.00"),
            transfer_type="account",
            destination="40817810000000000001"
        )

        with pytest.raises(HTTPException) as exc_info:
            await transfer_money(req)

        assert exc_info.value.status_code == 400
        assert "тот же самый счет невозможен" in exc_info.value.detail

    asyncio.run(_test())


def test_bank_transfer_rejects_inactive_accounts(monkeypatch):
    """
    Проверяет, что перевод блокируется, если отправитель или получатель неактивны.
    """
    async def _test():
        # 1. Заблокированный отправитель
        sender_blocked = Account(id="acc_blocked", account_number="NUM_B", balance=Decimal("500.00"), status="blocked")
        receiver = Account(id="acc_active", account_number="NUM_A", balance=Decimal("200.00"), status="active")

        session = AsyncMock()
        session.scalar = AsyncMock(return_value=receiver)
        res_mock = MagicMock()
        res_mock.scalars().all.return_value = [sender_blocked, receiver]
        session.execute = AsyncMock(return_value=res_mock)

        monkeypatch.setattr("src.simulations.api.routers.bank.get_db", lambda: make_async_cm(session))

        req = TransferRequest(
            from_account_id="acc_blocked",
            amount=Decimal("50.00"),
            transfer_type="account",
            destination="NUM_A"
        )
        with pytest.raises(HTTPException) as exc_info:
            await transfer_money(req)
        assert exc_info.value.status_code == 400
        assert "Счет отправителя недоступен" in exc_info.value.detail

        # 2. Закрытый получатель
        sender_active = Account(id="acc_active_1", account_number="NUM_A1", balance=Decimal("500.00"), status="active")
        receiver_closed = Account(id="acc_closed", account_number="NUM_C", balance=Decimal("0.00"), status="closed")

        session.scalar = AsyncMock(return_value=receiver_closed)
        res_mock.scalars().all.return_value = [sender_active, receiver_closed]

        req2 = TransferRequest(
            from_account_id="acc_active_1",
            amount=Decimal("50.00"),
            transfer_type="account",
            destination="NUM_C"
        )
        with pytest.raises(HTTPException) as exc_info2:
            await transfer_money(req2)
        assert exc_info2.value.status_code == 400
        assert "Счет получателя недоступен" in exc_info2.value.detail

    asyncio.run(_test())


def test_bank_close_account_rejects_positive_and_negative_balance(monkeypatch):
    """
    Проверяет, что закрытие счета невозможно как при положительном, так и при отрицательном балансе (долге).
    """
    async def _test():
        session = AsyncMock()
        monkeypatch.setattr("src.simulations.api.routers.bank.get_db", lambda: make_async_cm(session))

        # 1. Положительный баланс (+100.00)
        acc_pos = Account(id="acc_pos", balance=Decimal("100.00"), status="active")
        session.scalar = AsyncMock(return_value=acc_pos)

        with pytest.raises(HTTPException) as exc_pos:
            await close_account("acc_pos")
        assert exc_pos.value.status_code == 400
        assert "ненулевым балансом" in exc_pos.value.detail

        # 2. Отрицательный баланс / долг (-50.00)
        acc_neg = Account(id="acc_neg", balance=Decimal("-50.00"), status="active")
        session.scalar = AsyncMock(return_value=acc_neg)

        with pytest.raises(HTTPException) as exc_neg:
            await close_account("acc_neg")
        assert exc_neg.value.status_code == 400
        assert "ненулевым балансом" in exc_neg.value.detail

        # 3. Уже закрытый счет
        acc_closed = Account(id="acc_closed", balance=Decimal("0.00"), status="closed")
        session.scalar = AsyncMock(return_value=acc_closed)

        with pytest.raises(HTTPException) as exc_closed:
            await close_account("acc_closed")
        assert exc_closed.value.status_code == 400
        assert "Счет уже закрыт" in exc_closed.value.detail

        # 4. Успешное закрытие при нулевом балансе
        acc_zero = Account(id="acc_zero", balance=Decimal("0.00"), status="active")
        session.scalar = AsyncMock(return_value=acc_zero)
        session.commit = AsyncMock()

        res = await close_account("acc_zero")
        assert res["status"] == "success"
        assert acc_zero.status == "closed"

    asyncio.run(_test())


# ============================================================================
# 2. ТЕСТЫ ЦИФРОВОГО РУБЛЯ: САМОПЕРЕВОД И СМАРТ-КОНТРАКТЫ САМОМУ СЕБЕ
# ============================================================================

def test_ruble_transfer_rejects_self_transfer():
    """
    Проверяет, что P2P перевод цифровых рублей на тот же самый кошелек отклоняется с HTTP 400.
    """
    async def _test():
        req = RubleTransferRequest(to_wallet_id="w_same", amount=Decimal("150.00"))
        with pytest.raises(HTTPException) as exc_info:
            await transfer_rubles("w_same", req)

        assert exc_info.value.status_code == 400
        assert "тот же самый кошелек невозможен" in exc_info.value.detail

    asyncio.run(_test())


def test_ruble_transfer_rejects_inactive_wallets(monkeypatch):
    """
    Проверяет отклонение P2P перевода при неактивном статусе кошелька.
    """
    async def _test():
        session = AsyncMock()
        monkeypatch.setattr("src.simulations.api.routers.ruble.ruble_db", lambda: make_async_cm(session))

        w_blocked = Wallet(id="w_block", status="blocked", balance=Decimal("500.00"))
        w_act = Wallet(id="w_act", status="active", balance=Decimal("100.00"))

        res_mock = MagicMock()
        res_mock.scalars().all.return_value = [w_blocked, w_act]
        session.execute = AsyncMock(return_value=res_mock)

        req = RubleTransferRequest(to_wallet_id="w_act", amount=Decimal("50.00"))
        with pytest.raises(HTTPException) as exc_info:
            await transfer_rubles("w_block", req)
        assert exc_info.value.status_code == 400
        assert "Кошелек отправителя недоступен" in exc_info.value.detail

    asyncio.run(_test())


def test_ruble_create_contract_rejects_self_contract():
    """
    Проверяет, что создание смарт-контракта, где создатель равен получателю, отклоняется с HTTP 400.
    """
    async def _test():
        req = CreateContractRequest(
            receiver_wallet_id="w_creator_same",
            amount=Decimal("300.00"),
            condition_type="oracle",
            contract_code="def execute(ctx): pass"
        )
        with pytest.raises(HTTPException) as exc_info:
            await create_smart_contract("w_creator_same", req)

    asyncio.run(_test())


def test_bank_transfer_by_phone_selects_active_rub_account(monkeypatch):
    """
    Проверяет, что при переводе по номеру телефона выбирается активный рублевый счет.
    """
    async def _test():
        sender_acc = Account(id="sender_1", balance=Decimal("1000.00"), status="active", currency="RUB")
        receiver_active = Account(id="rec_active", client_id="cl_2", balance=Decimal("100.00"), status="active", currency="RUB")

        session = AsyncMock()
        # Поиск счета по телефону возвращает receiver_active
        session.scalar = AsyncMock(return_value=receiver_active)

        res_mock = MagicMock()
        res_mock.scalars().all.return_value = [sender_acc, receiver_active]
        session.execute = AsyncMock(return_value=res_mock)
        session.commit = AsyncMock()

        monkeypatch.setattr("src.simulations.api.routers.bank.get_db", lambda: make_async_cm(session))

        req = TransferRequest(
            from_account_id="sender_1",
            amount=Decimal("200.00"),
            transfer_type="phone",
            destination="cl_2"
        )
        res = await transfer_money(req)
        assert res["status"] == "success"
        assert sender_acc.balance == Decimal("800.00")
        assert receiver_active.balance == Decimal("300.00")

    asyncio.run(_test())


def test_bank_transfer_by_phone_rejects_when_no_active_rub_account(monkeypatch):
    """
    Проверяет, что если у получателя нет активных рублевых счетов, возвращается HTTP 400.
    """
    async def _test():
        session = AsyncMock()
        # 1-й вызов scalar (поиск активного счета) -> None
        # 2-й вызов scalar (проверка существования клиента) -> "acc_closed"
        session.scalar = AsyncMock(side_effect=[None, "acc_closed"])

        monkeypatch.setattr("src.simulations.api.routers.bank.get_db", lambda: make_async_cm(session))

        req = TransferRequest(
            from_account_id="sender_1",
            amount=Decimal("100.00"),
            transfer_type="phone",
            destination="cl_with_closed_accs"
        )
        with pytest.raises(HTTPException) as exc_info:
            await transfer_money(req)

        assert exc_info.value.status_code == 400
        assert "отсутствуют активные рублевые счета" in exc_info.value.detail

    asyncio.run(_test())

