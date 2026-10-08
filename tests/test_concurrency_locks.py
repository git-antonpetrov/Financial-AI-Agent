import asyncio
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from sqlalchemy.dialects import postgresql

from src.simulations.api.routers.bank import (
    transfer_money,
    close_account,
    TransferRequest,
)
from src.simulations.api.routers.ruble import (
    transfer_rubles,
    fund_wallet,
    withdraw_wallet,
    create_smart_contract,
    RubleTransferRequest,
    FundWithdrawRequest,
    CreateContractRequest,
)
from src.simulations.api.routers.invest import (
    open_deposit,
    open_savings,
    open_broker_account,
    close_deposit,
    close_savings,
    OpenProductRequest,
    CloseProductRequest,
)
from src.simulations.db.bank.db.models import Account
from src.simulations.db.digital_ruble.db.models import Wallet, SmartContract
from src.simulations.db.invest.db.models import Deposit, SavingsAccount, BrokerAccount


def compile_pg(stmt) -> str:
    """Компилирует SQLAlchemy Select в строку SQL диалекта PostgreSQL."""
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def make_async_cm(session):
    session.add = MagicMock()
    if hasattr(session, "scalar") and isinstance(session.scalar, AsyncMock):
        if isinstance(session.scalar.return_value, AsyncMock):
            session.scalar.return_value = None
    cm = MagicMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = None
    return cm


# ============================================================================
# 1. ТЕСТЫ БЛОКИРОВОК И ЗАЩИТЫ ОТ DEADLOCK В BANK ROUTER
# ============================================================================

def test_bank_transfer_money_uses_ordered_pessimistic_locking(monkeypatch):
    """
    Проверяет, что transfer_money выполняет SELECT ... ORDER BY accounts.id FOR UPDATE
    и сортирует ID счетов для предотвращения deadlocks при взаимных переводах.
    """
    async def _test():
        executed_statements = []

        # Создаем фейковые счета: acc_Z (отправитель) и acc_A (получатель)
        sender = Account(id="acc_Z", account_number="40817810000000000002", balance=Decimal("1000.00"), client_id="c_2")
        receiver = Account(id="acc_A", account_number="40817810000000000001", balance=Decimal("200.00"), client_id="c_1")

        session = AsyncMock()

        async def mock_scalar(stmt):
            executed_statements.append(("scalar", stmt))
            # Первый вызов ищет получателя по account_number
            return receiver

        async def mock_execute(stmt):
            executed_statements.append(("execute", stmt))
            # Возвращаем заблокированные строки
            result = MagicMock()
            result.scalars().all.return_value = [receiver, sender]
            return result

        session.scalar.side_effect = mock_scalar
        session.execute.side_effect = mock_execute
        session.commit = AsyncMock()

        monkeypatch.setattr("src.simulations.api.routers.bank.get_db", lambda: make_async_cm(session))

        req = TransferRequest(
            from_account_id="acc_Z",
            amount=Decimal("300.00"),
            transfer_type="account",
            destination="40817810000000000001"
        )

        await transfer_money(req)

        # Ищем execute-запрос с блокировкой
        lock_stmts = [stmt for op, stmt in executed_statements if op == "execute"]
        assert len(lock_stmts) == 1
        lock_stmt = lock_stmts[0]

        # Проверяем флаг FOR UPDATE
        assert lock_stmt._for_update_arg is not None
        assert lock_stmt._for_update_arg.read is False

        sql = compile_pg(lock_stmt)
        # Запрос должен содержать ORDER BY и FOR UPDATE
        assert "ORDER BY accounts.id" in sql
        assert "FOR UPDATE" in sql
        # Идентификаторы счетов отсортированы лексикографически ('acc_A', 'acc_Z')
        assert "'acc_A', 'acc_Z'" in sql or ("acc_A" in sql and "acc_Z" in sql)

        # Проверяем списание и зачисление
        assert sender.balance == Decimal("700.00")
        assert receiver.balance == Decimal("500.00")

    asyncio.run(_test())


def test_bank_transfer_money_deadlock_symmetry(monkeypatch):
    """
    Проверяет симметрию блокировок: при переводе A -> B и B -> A порядок
    запрашиваемых ID в SQL всегда строго одинаковый (детерминированный).
    """
    async def _test():
        captured_sqls = []

        for from_id, to_id in [("acc_2", "acc_1"), ("acc_1", "acc_2")]:
            acc1 = Account(id="acc_1", account_number="NUM_1", balance=Decimal("1000.00"), client_id="c_1")
            acc2 = Account(id="acc_2", account_number="NUM_2", balance=Decimal("1000.00"), client_id="c_2")
            acc_map = {"acc_1": acc1, "acc_2": acc2}

            session = AsyncMock()
            session.scalar = AsyncMock(return_value=acc_map[to_id])

            async def make_exec(stmt):
                captured_sqls.append(compile_pg(stmt))
                res = MagicMock()
                res.scalars().all.return_value = [acc1, acc2]
                return res

            session.execute = AsyncMock(side_effect=make_exec)
            session.commit = AsyncMock()

            monkeypatch.setattr("src.simulations.api.routers.bank.get_db", lambda: make_async_cm(session))

            await transfer_money(TransferRequest(
                from_account_id=from_id,
                amount=Decimal("100.00"),
                transfer_type="account",
                destination=f"NUM_{to_id[-1]}"
            ))

        # В обоих случаях запросы блокировки должны содержать одинаковый список идентификаторов в WHERE IN
        assert len(captured_sqls) == 2
        assert "ORDER BY accounts.id FOR UPDATE" in captured_sqls[0]
        assert "ORDER BY accounts.id FOR UPDATE" in captured_sqls[1]
        assert "WHERE accounts.id IN ('acc_1', 'acc_2')" in captured_sqls[0]
        assert "WHERE accounts.id IN ('acc_1', 'acc_2')" in captured_sqls[1]

    asyncio.run(_test())


def test_bank_close_account_uses_pessimistic_lock(monkeypatch):
    """
    Проверяет, что закрытие счета блокирует строку счета через with_for_update().
    """
    async def _test():
        acc = Account(id="acc_zero", account_number="NUM_ZERO", balance=Decimal("0.00"), status="active")
        session = AsyncMock()
        executed_stmt = None

        async def mock_scalar(stmt):
            nonlocal executed_stmt
            executed_stmt = stmt
            return acc

        session.scalar = AsyncMock(side_effect=mock_scalar)
        session.commit = AsyncMock()
        monkeypatch.setattr("src.simulations.api.routers.bank.get_db", lambda: make_async_cm(session))

        await close_account("acc_zero")

        assert executed_stmt is not None
        assert executed_stmt._for_update_arg is not None
        sql = compile_pg(executed_stmt)
        assert "FOR UPDATE" in sql
        assert acc.status == "closed"

    asyncio.run(_test())


# ============================================================================
# 2. ТЕСТЫ БЛОКИРОВОК И ЗАЩИТЫ ОТ DEADLOCK В RUBLE ROUTER
# ============================================================================

def test_ruble_transfer_uses_ordered_pessimistic_locking(monkeypatch):
    """
    Проверяет, что transfer_rubles выполняет SELECT ... ORDER BY wallets.id FOR UPDATE
    и сортирует ID кошельков для исключения deadlock.
    """
    async def _test():
        sender = Wallet(id="w_Z", client_id="cl_2", balance=Decimal("500.00"), frozen_balance=Decimal("0.00"))
        receiver = Wallet(id="w_A", client_id="cl_1", balance=Decimal("100.00"), frozen_balance=Decimal("0.00"))

        session = AsyncMock()
        executed_stmt = None

        async def mock_execute(stmt):
            nonlocal executed_stmt
            executed_stmt = stmt
            res = MagicMock()
            res.scalars().all.return_value = [receiver, sender]
            return res

        session.execute = AsyncMock(side_effect=mock_execute)
        session.commit = AsyncMock()
        session.refresh = AsyncMock()

        monkeypatch.setattr("src.simulations.api.routers.ruble.ruble_db", lambda: make_async_cm(session))

        req = RubleTransferRequest(to_wallet_id="w_A", amount=Decimal("200.00"))
        await transfer_rubles("w_Z", req)

        assert executed_stmt is not None
        assert executed_stmt._for_update_arg is not None
        sql = compile_pg(executed_stmt)
        assert "ORDER BY wallets.id" in sql
        assert "FOR UPDATE" in sql
        assert "'w_A', 'w_Z'" in sql or ("w_A" in sql and "w_Z" in sql)

        assert sender.balance == Decimal("300.00")
        assert receiver.balance == Decimal("300.00")

    asyncio.run(_test())


def test_ruble_fund_and_withdraw_use_pessimistic_locks(monkeypatch):
    """
    Проверяет, что fund_wallet и withdraw_wallet используют with_for_update()
    как для банковского счета, так и для цифрового кошелька.
    """
    async def _test():
        acc = Account(id="bank_acc_1", balance=Decimal("1000.00"))
        wallet = Wallet(id="ruble_w_1", balance=Decimal("500.00"), frozen_balance=Decimal("0.00"))

        # 1. Пополнение кошелька (fund_wallet)
        bank_stmts = []
        ruble_stmts = []

        bank_session = AsyncMock()
        async def bank_scalar(stmt):
            bank_stmts.append(stmt)
            return acc
        bank_session.scalar = AsyncMock(side_effect=bank_scalar)
        bank_session.commit = AsyncMock()

        ruble_session = AsyncMock()
        async def ruble_scalar(stmt):
            ruble_stmts.append(stmt)
            return wallet
        ruble_session.scalar = AsyncMock(side_effect=ruble_scalar)
        ruble_session.commit = AsyncMock()

        monkeypatch.setattr("src.simulations.api.routers.ruble.bank_db", lambda: make_async_cm(bank_session))
        monkeypatch.setattr("src.simulations.api.routers.ruble.ruble_db", lambda: make_async_cm(ruble_session))

        req = FundWithdrawRequest(bank_account_id="bank_acc_1", amount=Decimal("250.00"))
        await fund_wallet("ruble_w_1", req)

        assert len(bank_stmts) == 1
        assert bank_stmts[0]._for_update_arg is not None
        assert "FOR UPDATE" in compile_pg(bank_stmts[0])

        assert len(ruble_stmts) == 1
        assert ruble_stmts[0]._for_update_arg is not None
        assert "FOR UPDATE" in compile_pg(ruble_stmts[0])

        # 2. Вывод из кошелька (withdraw_wallet)
        bank_stmts.clear()
        ruble_stmts.clear()

        await withdraw_wallet("ruble_w_1", req)

        assert len(bank_stmts) == 1
        assert bank_stmts[0]._for_update_arg is not None
        assert "FOR UPDATE" in compile_pg(bank_stmts[0])

        assert len(ruble_stmts) == 1
        assert ruble_stmts[0]._for_update_arg is not None
        assert "FOR UPDATE" in compile_pg(ruble_stmts[0])

    asyncio.run(_test())


def test_ruble_create_smart_contract_locks_creator_wallet(monkeypatch):
    """
    Проверяет, что при создании смарт-контракта кошелек создателя блокируется через with_for_update().
    """
    async def _test():
        wallet = Wallet(id="creator_w", balance=Decimal("1000.00"), frozen_balance=Decimal("0.00"))
        session = AsyncMock()
        executed_stmt = None

        async def mock_scalar(stmt):
            nonlocal executed_stmt
            executed_stmt = stmt
            return wallet

        session.scalar = AsyncMock(side_effect=mock_scalar)
        session.commit = AsyncMock()
        session.refresh = AsyncMock()
        monkeypatch.setattr("src.simulations.api.routers.ruble.ruble_db", lambda: make_async_cm(session))

        req = CreateContractRequest(
            receiver_wallet_id="receiver_w",
            amount=Decimal("400.00"),
            condition_type="oracle",
            contract_code="def execute(ctx): pass"
        )
        await create_smart_contract("creator_w", req)

        assert executed_stmt is not None
        assert executed_stmt._for_update_arg is not None
        assert "FOR UPDATE" in compile_pg(executed_stmt)
        assert wallet.balance == Decimal("600.00")
        assert wallet.frozen_balance == Decimal("400.00")

    asyncio.run(_test())


# ============================================================================
# 3. ТЕСТЫ БЛОКИРОВОК В INVEST ROUTER
# ============================================================================

def test_invest_open_products_lock_bank_account(monkeypatch):
    """
    Проверяет, что при открытии вклада, копилки или брокерского счета
    банковский счет блокируется через with_for_update().
    """
    async def _test():
        acc = Account(id="bank_acc_invest", balance=Decimal("50000.00"))

        bank_stmts = []
        bank_session = AsyncMock()
        async def bank_scalar(stmt):
            bank_stmts.append(stmt)
            return acc
        bank_session.scalar = AsyncMock(side_effect=bank_scalar)
        bank_session.commit = AsyncMock()

        invest_session = AsyncMock()
        invest_session.commit = AsyncMock()
        invest_session.refresh = AsyncMock()

        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: make_async_cm(bank_session))
        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: make_async_cm(invest_session))

        # 1. Открытие вклада
        req_dep = OpenProductRequest(client_id="cl_1", from_bank_account_id="bank_acc_invest", initial_amount=Decimal("10000.00"), term_months=6)
        await open_deposit(req_dep)
        assert bank_stmts[-1]._for_update_arg is not None
        assert "FOR UPDATE" in compile_pg(bank_stmts[-1])

        # 2. Открытие накопительного счета
        req_sav = OpenProductRequest(client_id="cl_1", from_bank_account_id="bank_acc_invest", initial_amount=Decimal("5000.00"))
        await open_savings(req_sav)
        assert bank_stmts[-1]._for_update_arg is not None
        assert "FOR UPDATE" in compile_pg(bank_stmts[-1])

        # 3. Открытие брокерского счета
        req_brk = OpenProductRequest(client_id="cl_1", from_bank_account_id="bank_acc_invest", initial_amount=Decimal("15000.00"))
        await open_broker_account(req_brk)
        assert bank_stmts[-1]._for_update_arg is not None
        assert "FOR UPDATE" in compile_pg(bank_stmts[-1])

    asyncio.run(_test())


def test_invest_close_products_lock_both_records(monkeypatch):
    """
    Проверяет, что при закрытии вклада и копилки блокируются как запись инвестиций,
    так и целевой банковский счет через with_for_update().
    """
    async def _test():
        dep = Deposit(id="dep_1", client_id="cl_1", balance=Decimal("12000.00"), status="active")
        sav = SavingsAccount(id="sav_1", client_id="cl_1", balance=Decimal("7000.00"), status="active")
        acc = Account(id="bank_acc_target", balance=Decimal("1000.00"))

        invest_stmts = []
        invest_session = AsyncMock()
        async def invest_scalar(stmt):
            invest_stmts.append(stmt)
            if "deposits" in compile_pg(stmt):
                return dep
            return sav
        invest_session.scalar = AsyncMock(side_effect=invest_scalar)
        invest_session.commit = AsyncMock()

        bank_stmts = []
        bank_session = AsyncMock()
        async def bank_scalar(stmt):
            bank_stmts.append(stmt)
            return acc
        bank_session.scalar = AsyncMock(side_effect=bank_scalar)
        bank_session.commit = AsyncMock()

        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: make_async_cm(invest_session))
        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: make_async_cm(bank_session))

        req = CloseProductRequest(to_bank_account_id="bank_acc_target")

        # Закрытие вклада
        await close_deposit("dep_1", req)
        assert invest_stmts[-1]._for_update_arg is not None
        assert "FOR UPDATE" in compile_pg(invest_stmts[-1])
        assert bank_stmts[-1]._for_update_arg is not None
        assert "FOR UPDATE" in compile_pg(bank_stmts[-1])

        # Закрытие копилки
        await close_savings("sav_1", req)
        assert invest_stmts[-1]._for_update_arg is not None
        assert "FOR UPDATE" in compile_pg(invest_stmts[-1])
        assert bank_stmts[-1]._for_update_arg is not None
        assert "FOR UPDATE" in compile_pg(bank_stmts[-1])

    asyncio.run(_test())
