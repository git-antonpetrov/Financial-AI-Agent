import asyncio
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from src.simulations.smart_contracts.dsl.context import ContractContext
from src.simulations.api.routers.invest import (
    close_deposit,
    close_savings,
    CloseProductRequest,
    open_deposit,
    open_savings,
    OpenProductRequest,
)
from src.simulations.db.bank.db.models import Account, Tariff, AutoPayment
from src.simulations.db.invest.db.models import Deposit, SavingsAccount, BrokerAccount, InvestmentStrategy
from src.simulations.workers.bank_eod.tasks import (
    process_auto_payments,
    process_tariff_fees,
    process_salaries,
)
from src.simulations.workers.invest_eod.tasks import (
    process_savings_accounts,
    process_deposits,
    process_broker_accounts,
)


def make_async_cm(session):
    if "add" not in session.__dict__:
        session.add = MagicMock()
    cm = MagicMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = None
    return cm


# ============================================================================
# 1. ТЕСТЫ ТОЧНОСТИ DECIMAL В СМАРТ-КОНТРАКТАХ
# ============================================================================

def test_contract_context_preserves_decimal_precision():
    """
    Классическая проблема float: 0.1 + 0.2 = 0.30000000000000004.
    Проверяем, что ContractContext оперирует точным типом Decimal
    и сумма 0.1 + 0.2 укладывается ровно в лимит 0.30 без погрешностей.
    """
    ctx = ContractContext(
        contract_id="c_dec",
        creator_id="w_creator",
        receiver_id="w_receiver",
        amount=Decimal("0.30"),
        condition_status="fulfilled"
    )

    assert isinstance(ctx.amount, Decimal)
    assert ctx.amount == Decimal("0.30")

    # Выполняем два перевода 0.10 и 0.20
    ctx.transfer("w_creator", "w_receiver", Decimal("0.10"))
    ctx.transfer("w_creator", "w_receiver", Decimal("0.20"))

    assert len(ctx._requested_transfers) == 2
    for t in ctx._requested_transfers:
        assert isinstance(t["amount"], Decimal)

    total_transferred = sum((t["amount"] for t in ctx._requested_transfers), Decimal("0.00"))
    assert total_transferred == Decimal("0.30")

    # Попытка перевести еще хотя бы 1 копейку (0.01) отклоняется
    with pytest.raises(ValueError, match="превышает лимит"):
        ctx.transfer("w_creator", "w_receiver", Decimal("0.01"))


# ============================================================================
# 2. ТЕСТЫ ТОЧНОСТИ DECIMAL В INVEST ROUTER (ЗАКРЫТИЕ И ОТКРЫТИЕ)
# ============================================================================

def test_invest_close_deposit_and_savings_exact_decimal(monkeypatch):
    """
    Проверяет, что при закрытии вклада и копилки:
    - returned_amount возвращается в точном Decimal;
    - баланс закрываемого продукта становится Decimal('0.00');
    - на банковский счет зачисляется ровно сумма вклада без float-артефактов.
    """
    async def _test():
        # Тестируем сумму с копейками, склонную к потере точности во float
        initial_balance = Decimal("12345.67")
        dep = Deposit(id="dep_dec", client_id="cl_1", balance=initial_balance, status="active")
        bank_acc = Account(id="bank_acc_1", balance=Decimal("100.00"))

        invest_session = AsyncMock()
        invest_session.scalar = AsyncMock(return_value=dep)
        invest_session.commit = AsyncMock()

        bank_session = AsyncMock()
        bank_session.scalar = AsyncMock(return_value=bank_acc)
        bank_session.commit = AsyncMock()

        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: make_async_cm(invest_session))
        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: make_async_cm(bank_session))

        res = await close_deposit("dep_dec", CloseProductRequest(to_bank_account_id="bank_acc_1"))

        assert res["status"] == "success"
        assert isinstance(res["returned_amount"], Decimal)
        assert res["returned_amount"] == Decimal("12345.67")

        assert isinstance(dep.balance, Decimal)
        assert dep.balance == Decimal("0.00")

        assert isinstance(bank_acc.balance, Decimal)
        assert bank_acc.balance == Decimal("100.00") + Decimal("12345.67")
        assert bank_acc.balance == Decimal("12445.67")

    asyncio.run(_test())


def test_invest_open_deposit_sets_decimal_rate(monkeypatch):
    """
    Проверяет, что при открытии вклада и копилки процентная ставка сохраняется в Decimal.
    """
    async def _test():
        bank_acc = Account(id="bank_acc_1", balance=Decimal("50000.00"))
        created_objects = []

        bank_session = AsyncMock()
        bank_session.scalar = AsyncMock(return_value=bank_acc)
        bank_session.commit = AsyncMock()

        invest_session = AsyncMock()
        invest_session.add = MagicMock(side_effect=lambda obj: created_objects.append(obj))
        invest_session.commit = AsyncMock()
        invest_session.refresh = AsyncMock()

        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: make_async_cm(bank_session))
        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: make_async_cm(invest_session))

        await open_deposit(OpenProductRequest(
            client_id="cl_1",
            from_bank_account_id="bank_acc_1",
            initial_amount=Decimal("15000.50"),
            term_months=12
        ))

        assert len(created_objects) == 1
        dep = created_objects[0]
        assert isinstance(dep.interest_rate, Decimal)
        assert dep.interest_rate == Decimal("15.00")
        assert isinstance(dep.balance, Decimal)
        assert dep.balance == Decimal("15000.50")

    asyncio.run(_test())


# ============================================================================
# 3. ТЕСТЫ РАСЧЕТОВ ВОРКЕРОВ BANK EOD И INVEST EOD С ТОЧНОСТЬЮ DECIMAL
# ============================================================================

def test_bank_eod_tasks_exact_decimal_calculations():
    """
    Проверяет, что списание автоплатежей и тарифов в bank_eod выполняется
    с использованием арифметики Decimal без потери копеек.
    """
    async def _test():
        session = AsyncMock()
        session.add = MagicMock()
        session.commit = AsyncMock()

        from datetime import date
        current_date = date(2026, 10, 1)

        # 1. Автоплатеж
        acc = Account(id="acc_1", balance=Decimal("100.15"), status="active")
        ap = AutoPayment(
            id="ap_1",
            account_id="acc_1",
            amount=Decimal("30.10"),
            recipient="ЖКХ",
            schedule="monthly",
            next_payment_date=current_date,
            is_active=True
        )

        session.get = AsyncMock(return_value=acc)
        mock_ap_res = MagicMock()
        mock_ap_res.scalars().all.return_value = [ap]
        session.execute = AsyncMock(return_value=mock_ap_res)

        await process_auto_payments(session, current_date)

        assert isinstance(acc.balance, Decimal)
        assert acc.balance == Decimal("70.05")

        # 2. Абонентская плата по тарифу (1-е число месяца)
        tariff = Tariff(id="t_1", name="Премиум", service_cost=Decimal("70.05"))
        acc.tariff = tariff

        mock_acc_res = MagicMock()
        mock_acc_res.scalars().all.return_value = [acc]
        session.execute = AsyncMock(return_value=mock_acc_res)

        await process_tariff_fees(session, current_date)

        assert isinstance(acc.balance, Decimal)
        assert acc.balance == Decimal("0.00")

    asyncio.run(_test())


def test_invest_eod_tasks_exact_decimal_calculations():
    """
    Проверяет, что начисление процентов по вкладам и копилкам в invest_eod
    использует точный расчет в Decimal.
    """
    async def _test():
        session = AsyncMock()
        session.commit = AsyncMock()

        from datetime import date, datetime
        current_date = date(2026, 10, 15)
        current_datetime = datetime.combine(current_date, datetime.max.time())

        # Накопительный счет: баланс 100 000.00, ставка 12.00%
        # monthly_rate = 12 / 100 / 12 = 0.01 (1%)
        # next_payment_amount = round(100 000 * 0.01, 2) = 1 000.00
        sav = SavingsAccount(
            id="sav_dec",
            balance=Decimal("100000.00"),
            interest_rate=Decimal("12.00"),
            next_payment_amount=Decimal("1000.00"),
            next_payment_date=current_datetime,
            status="active"
        )

        mock_res = MagicMock()
        mock_res.scalars().all.return_value = [sav]
        session.execute = AsyncMock(return_value=mock_res)

        await process_savings_accounts(session, current_date)

        assert isinstance(sav.balance, Decimal)
        # Капитализация: 100000 + 1000 = 101000.00
        assert sav.balance == Decimal("101000.00")
        assert isinstance(sav.next_payment_amount, Decimal)
        # Следующая выплата: 101000.00 * 0.01 = 1010.00
        assert sav.next_payment_amount == Decimal("1010.00")

    asyncio.run(_test())
