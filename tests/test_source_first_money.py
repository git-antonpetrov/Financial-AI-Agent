"""Проверяет порядок межкнижных проводок, откат закрытия и идемпотентность автоплатежа."""

import asyncio
import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from src.simulations.api.routers.bank import AutoPaymentCreate, TransferRequest, create_autopayment, transfer_money
from src.simulations.api.routers.invest import (
    CloseProductRequest,
    OpenProductRequest,
    close_deposit,
    close_savings,
    open_deposit,
)
from src.simulations.api.routers.ruble import FundWithdrawRequest, fund_wallet, withdraw_wallet
from src.simulations.db.bank.db.models import Account, AutoPayment, BankIdempotencyClaim, IdempotencyRecord, Transaction
from src.simulations.db.digital_ruble.db.models import RubleTransaction, Wallet
from src.simulations.db.invest.db.models import BrokerAccount, Deposit, SavingsAccount
from src.simulations.workers.bank_eod.tasks import process_auto_payments
from src.simulations.workers.invest_eod.tasks import process_broker_accounts, process_deposits, process_savings_accounts


def compile_pg(stmt) -> str:
    """Компилирует выражение SQLAlchemy в текст PostgreSQL."""
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def context(session):
    """Открывает переданную сессию как асинхронный контекст."""
    manager = MagicMock()
    manager.__aenter__.return_value = session
    manager.__aexit__.return_value = None
    return manager


def row_matches(row: object, sql: str) -> bool:
    """Сообщает, что строка относится к тексту запроса."""
    table = getattr(getattr(row, "__table__", None), "name", "")
    if not table or table not in sql:
        return False
    claim_key = getattr(row, "key", None) if table == "bank_idempotency_claims" else None
    key = getattr(row, "idempotency_key", None)
    close_key = getattr(row, "close_idempotency_key", None)
    row_id = getattr(row, "id", None)
    if claim_key and f"{table}.key" in sql and str(claim_key) in sql:
        return True
    if key and "idempotency_key" in sql and str(key) in sql:
        return True
    if close_key and "close_idempotency_key" in sql and str(close_key) in sql:
        return True
    return bool(row_id) and str(row_id) in sql


class MemorySession:
    """Хранит строки в памяти и отдаёт их по таблице и ключу из SQL."""

    def __init__(self) -> None:
        self.rows: list[object] = []

    def add(self, row: object) -> None:
        """Запоминает строку, если её ещё нет в сессии."""
        if row not in self.rows:
            self.rows.append(row)

    async def delete(self, row: object) -> None:
        """Удаляет строку из памяти."""
        if row in self.rows:
            self.rows.remove(row)

    async def commit(self) -> None:
        """Фиксирует сессию без базы."""
        return None

    async def flush(self) -> None:
        """Назначает идентификаторы новых строк до коммита."""
        for row in self.rows:
            if getattr(row, "id", None) is None:
                row.id = str(uuid.uuid4())

    async def rollback(self) -> None:
        """Откатывает сессию без базы."""
        return None

    async def refresh(self, row: object) -> None:
        """Оставляет строку как есть: идентификатор уже задан кодом."""
        if getattr(row, "id", None) is None:
            row.id = str(uuid.uuid4())

    async def get(self, model, ident, **_kwargs):
        """Возвращает строку модели с этим идентификатором."""
        for row in self.rows:
            if isinstance(row, model) and str(getattr(row, "id", "")) == str(ident):
                return row
        return None

    async def scalar(self, stmt) -> object | None:
        """Возвращает первую строку, которую описывает запрос."""
        sql = compile_pg(stmt)
        for row in self.rows:
            if row_matches(row, sql):
                return row
        return None

    async def execute(self, stmt):
        """Возвращает автоплатежи, если запрос читает их таблицу."""
        sql = compile_pg(stmt)
        found = [row for row in self.rows if isinstance(row, AutoPayment) and "auto_payments" in sql]
        result = MagicMock()
        result.scalars.return_value.all.return_value = found
        return result


def test_fund_debits_bank_before_wallet_and_retry_credits_once(monkeypatch):
    """Списывает банк до роста кошелька. Повтор после списания только зачисляет кошелёк."""

    async def _test():
        account = Account(
            id="bank_fund_order",
            client_id="client_1",
            balance=Decimal("100.00"),
            currency="RUB",
            status="active",
        )
        wallet = Wallet(id="wallet_fund_order", client_id="client_1", balance=Decimal("100.00"), status="active")
        bank = MemorySession()
        ruble = MemorySession()
        bank.rows.append(account)
        ruble.rows.append(wallet)
        seen: list[tuple[Decimal, Decimal]] = []

        async def ruble_commit() -> None:
            if any(isinstance(row, RubleTransaction) for row in ruble.rows):
                seen.append((account.balance, wallet.balance))

        ruble.commit = ruble_commit
        monkeypatch.setattr("src.simulations.api.routers.ruble.bank_db", lambda: context(bank))
        monkeypatch.setattr("src.simulations.api.routers.ruble.ruble_db", lambda: context(ruble))
        request = FundWithdrawRequest(
            bank_account_id="bank_fund_order",
            amount=Decimal("30.00"),
            idempotency_key="fund-source-first",
        )

        await fund_wallet("wallet_fund_order", request)
        account.balance = Decimal("0.00")
        wallet.balance = Decimal("100.00")
        ruble.rows[:] = [wallet]
        await fund_wallet("wallet_fund_order", request)

        assert seen[0] == (Decimal("70.00"), Decimal("130.00"))
        assert account.balance == Decimal("0.00")
        assert wallet.balance == Decimal("130.00")
        assert len([row for row in bank.rows if isinstance(row, Transaction)]) == 1

    asyncio.run(_test())


def test_withdraw_debits_wallet_before_bank_and_retry_credits_once(monkeypatch):
    """Списывает кошелёк до роста банка. Повтор после списания только зачисляет банк."""

    async def _test():
        account = Account(
            id="bank_wd_order",
            client_id="client_1",
            balance=Decimal("10.00"),
            currency="RUB",
            status="active",
        )
        wallet = Wallet(id="wallet_wd_order", client_id="client_1", balance=Decimal("80.00"), status="active")
        bank = MemorySession()
        ruble = MemorySession()
        bank.rows.append(account)
        ruble.rows.append(wallet)
        order: list[str] = []

        async def bank_commit() -> None:
            if any(isinstance(row, Transaction) for row in bank.rows):
                order.append(f"bank:{account.balance}:{wallet.balance}")

        async def ruble_commit() -> None:
            if any(isinstance(row, RubleTransaction) for row in ruble.rows):
                order.append(f"wallet:{account.balance}:{wallet.balance}")

        bank.commit = bank_commit
        ruble.commit = ruble_commit
        monkeypatch.setattr("src.simulations.api.routers.ruble.bank_db", lambda: context(bank))
        monkeypatch.setattr("src.simulations.api.routers.ruble.ruble_db", lambda: context(ruble))
        request = FundWithdrawRequest(
            bank_account_id="bank_wd_order",
            amount=Decimal("25.00"),
            idempotency_key="withdraw-source-first",
        )

        await withdraw_wallet("wallet_wd_order", request)
        for row in list(bank.rows):
            if isinstance(row, (Transaction, BankIdempotencyClaim)):
                bank.rows.remove(row)
        for row in ruble.rows:
            if isinstance(row, RubleTransaction):
                row.response_payload = None
        account.balance = Decimal("10.00")
        await withdraw_wallet("wallet_wd_order", request)

        assert order[0] == "wallet:10.00:55.00"
        assert order[1] == "bank:35.00:55.00"
        assert account.balance == Decimal("35.00")
        assert wallet.balance == Decimal("55.00")
        assert len([row for row in bank.rows if isinstance(row, Transaction)]) == 1

    asyncio.run(_test())


def test_unpaid_wallet_credit_is_not_spendable_before_bank_debit(monkeypatch):
    """Снимает зачисление кошелька, если банк ещё не списан, и проводит сумму один раз."""

    async def _test():
        account = Account(
            id="bank_unpaid",
            client_id="client_1",
            balance=Decimal("100.00"),
            currency="RUB",
            status="active",
        )
        wallet = Wallet(id="wallet_unpaid", client_id="client_1", balance=Decimal("130.00"), status="active")
        premature = RubleTransaction(
            id="rtx_unpaid",
            receiver_wallet_id="wallet_unpaid",
            amount=Decimal("30.00"),
            idempotency_key="fund-unpaid",
            status="completed",
        )
        bank = MemorySession()
        ruble = MemorySession()
        bank.rows.append(account)
        ruble.rows.extend([wallet, premature])
        monkeypatch.setattr("src.simulations.api.routers.ruble.bank_db", lambda: context(bank))
        monkeypatch.setattr("src.simulations.api.routers.ruble.ruble_db", lambda: context(ruble))
        request = FundWithdrawRequest(
            bank_account_id="bank_unpaid",
            amount=Decimal("30.00"),
            idempotency_key="fund-unpaid",
        )

        await fund_wallet("wallet_unpaid", request)

        assert account.balance == Decimal("70.00")
        assert wallet.balance == Decimal("130.00")

    asyncio.run(_test())


def test_open_deposit_debits_before_product_and_retry_creates_it_once(monkeypatch):
    """Списывает банк до появления активного вклада. Повтор только создаёт вклад."""

    async def _test():
        account = Account(
            id="bank_dep_order",
            client_id="client_1",
            balance=Decimal("200.00"),
            currency="RUB",
            status="active",
        )
        bank = MemorySession()
        invest = MemorySession()
        bank.rows.append(account)
        active_at_commit: list[Decimal] = []

        async def invest_commit() -> None:
            active = [row for row in invest.rows if isinstance(row, Deposit) and row.status == "active"]
            if active:
                active_at_commit.append(account.balance)

        invest.commit = invest_commit
        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: context(bank))
        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: context(invest))
        request = OpenProductRequest(
            client_id="client_1",
            from_bank_account_id="bank_dep_order",
            initial_amount=Decimal("50.00"),
            term_months=12,
            idempotency_key="deposit-source-first",
        )

        first = await open_deposit(request)
        invest.rows.clear()
        await open_deposit(request)

        deposits = [row for row in invest.rows if isinstance(row, Deposit)]
        assert active_at_commit[0] == Decimal("150.00")
        assert account.balance == Decimal("150.00")
        assert len(deposits) == 1
        assert deposits[0].status == "active"
        assert deposits[0].balance == Decimal("50.00")
        assert first.balance == Decimal("50.00")

    asyncio.run(_test())


def test_unpaid_products_are_not_paid_by_close_or_eod(monkeypatch):
    """Не оплачивает неоплаченные вклад, копилку и брокерский счёт закрытием или EOD."""

    async def _test():
        deposit = Deposit(
            id="dep_unpaid",
            client_id="client_1",
            balance=Decimal("500.00"),
            status="unfunded",
            interest_rate=Decimal("15.00"),
            term_months=6,
            next_payment_amount=Decimal("10.00"),
        )
        savings = SavingsAccount(
            id="sav_unpaid",
            client_id="client_1",
            balance=Decimal("80.00"),
            status="unfunded",
            interest_rate=Decimal("10.00"),
            next_payment_amount=Decimal("5.00"),
        )
        broker = BrokerAccount(
            id="brk_unpaid",
            client_id="client_1",
            balance=Decimal("40.00"),
            status="unfunded",
        )
        account = Account(
            id="bank_unpaid_close",
            client_id="client_1",
            balance=Decimal("100.00"),
            currency="RUB",
            status="active",
        )
        invest = MemorySession()
        bank = MemorySession()
        invest.rows.extend([deposit, savings, broker])
        bank.rows.append(account)
        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: context(invest))
        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: context(bank))

        async def paid(*_args, **_kwargs):
            raise AssertionError("EOD выплатил неоплаченный продукт")

        monkeypatch.setattr("src.simulations.workers.invest_eod.tasks.return_deposit_to_bank", paid)
        request = CloseProductRequest(to_bank_account_id="bank_unpaid_close", idempotency_key="close-unpaid")
        with pytest.raises(HTTPException) as deposit_error:
            await close_deposit("dep_unpaid", request)
        with pytest.raises(HTTPException) as savings_error:
            await close_savings("sav_unpaid", request)

        eod = AsyncMock()
        eod.commit = AsyncMock()
        deposit_result = MagicMock()
        deposit_result.scalars.return_value.all.return_value = [deposit]
        savings_result = MagicMock()
        savings_result.scalars.return_value.all.return_value = [savings]
        broker_result = MagicMock()
        broker_result.scalars.return_value.all.return_value = [broker]
        eod.execute = AsyncMock(side_effect=[deposit_result, savings_result, broker_result])
        current = date(2026, 11, 5)
        await process_deposits(eod, current)
        await process_savings_accounts(eod, current)
        await process_broker_accounts(eod, current)

        assert deposit_error.value.status_code == 409
        assert savings_error.value.status_code == 409
        assert "не оплачен" in str(deposit_error.value.detail)
        assert account.balance == Decimal("100.00")
        assert deposit.balance == Decimal("500.00")
        assert deposit.status == "unfunded"
        assert savings.balance == Decimal("80.00")
        assert broker.balance == Decimal("40.00")

    asyncio.run(_test())


def test_close_deposit_does_not_reverse_when_product_is_already_closed(monkeypatch):
    """Не уменьшает банк, если вклад уже закрыт после зачисления этого запроса."""

    async def _test():
        active = Deposit(id="dep_race", client_id="client_1", balance=Decimal("40.00"), status="active")
        closed = Deposit(
            id="dep_race",
            client_id="client_1",
            balance=Decimal("0.00"),
            status="closed",
            payout_key="close-race",
        )
        account = Account(
            id="bank_race",
            client_id="client_1",
            balance=Decimal("100.00"),
            currency="RUB",
            status="active",
        )
        calls = {"n": 0}

        invest = AsyncMock()

        async def invest_scalar(_stmt):
            calls["n"] += 1
            if calls["n"] == 1:
                return active
            return closed

        invest.scalar = invest_scalar
        invest.commit = AsyncMock()
        bank = AsyncMock()
        bank.scalar = AsyncMock(return_value=account)
        bank.add = MagicMock()
        bank.delete = AsyncMock()
        bank.commit = AsyncMock()
        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: context(invest))
        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: context(bank))
        request = CloseProductRequest(to_bank_account_id="bank_race", idempotency_key="close-race")

        with pytest.raises(HTTPException) as exc_info:
            await close_deposit("dep_race", request)

        assert exc_info.value.status_code == 409
        assert account.balance == Decimal("140.00")

    asyncio.run(_test())


def test_close_savings_commit_failure_reverses_only_this_credit(monkeypatch):
    """Возвращает выплату копилки, если закрытие после зачисления не сохранилось."""

    async def _test():
        savings = SavingsAccount(id="sav_fail", client_id="client_1", balance=Decimal("15.00"), status="active")
        account = Account(
            id="bank_sav_fail",
            client_id="client_1",
            balance=Decimal("20.00"),
            currency="RUB",
            status="active",
        )
        commits = {"n": 0}

        async def invest_commit():
            commits["n"] += 1
            if commits["n"] >= 2:
                raise RuntimeError("закрытие копилки не записано")

        invest = AsyncMock()
        invest.scalar = AsyncMock(return_value=savings)
        invest.commit = invest_commit
        added = []
        bank = AsyncMock()

        async def bank_scalar(stmt):
            sql = str(stmt)
            if "bank_idempotency_claims" in sql:
                for row in added:
                    if getattr(row, "key", None) == "savings-close-fail":
                        return row
                return None
            return account

        bank.scalar = bank_scalar
        bank.add = MagicMock(side_effect=added.append)
        bank.delete = AsyncMock()
        bank.commit = AsyncMock()
        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: context(invest))
        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: context(bank))
        request = CloseProductRequest(to_bank_account_id="bank_sav_fail", idempotency_key="savings-close-fail")

        with pytest.raises(HTTPException) as exc_info:
            await close_savings("sav_fail", request)

        assert exc_info.value.status_code == 500
        assert savings.status == "active"
        assert account.balance == Decimal("20.00")

    asyncio.run(_test())


def test_create_autopayment_requires_key_and_eod_debits_once(monkeypatch):
    """Повтор с тем же ключом не создаёт второй автоплатёж, и EOD списывает его один раз."""

    async def _test():
        session = MemorySession()
        account = Account(id="acc_auto", balance=Decimal("100.00"), status="active")
        session.rows.append(account)
        monkeypatch.setattr("src.simulations.api.routers.bank.get_db", lambda: context(session))
        payload = dict(
            amount=Decimal("30.00"),
            recipient="ЖКХ",
            schedule="monthly",
            next_payment_date=date(2026, 11, 1),
            idempotency_key="autopay-once",
        )

        with pytest.raises(HTTPException) as missing:
            await create_autopayment("acc_auto", AutoPaymentCreate(**{**payload, "idempotency_key": None}))
        first = await create_autopayment("acc_auto", AutoPaymentCreate(**payload))
        second = await create_autopayment("acc_auto", AutoPaymentCreate(**payload))
        with pytest.raises(HTTPException) as conflict:
            await create_autopayment(
                "acc_auto",
                AutoPaymentCreate(**{**payload, "amount": Decimal("31.00")}),
            )

        payments = [row for row in session.rows if isinstance(row, AutoPayment)]
        current = date(2026, 11, 5)
        await process_auto_payments(session, current)
        await process_auto_payments(session, current)

        assert missing.value.status_code == 400
        assert conflict.value.status_code == 409
        assert first.id == second.id
        assert len(payments) == 1
        assert account.balance == Decimal("70.00")
        assert len([row for row in session.rows if isinstance(row, Transaction)]) == 1

    asyncio.run(_test())


def test_claim_and_payout_amounts_are_encrypted_before_sql():
    """Проверяет, что сумма заявки и сумма выплаты шифруются до отправки в SQL."""
    from src.simulations.core.crypto.at_rest import is_encrypted_text

    claim_bound = BankIdempotencyClaim.__table__.c.amount.type.process_bind_param(Decimal("10.50"), None)
    payout_bound = Deposit.__table__.c.payout_amount.type.process_bind_param(Decimal("70700.00"), None)
    assert is_encrypted_text(claim_bound)
    assert "10.50" not in claim_bound
    assert is_encrypted_text(payout_bound)
    assert "70700.00" not in payout_bound
    restored = BankIdempotencyClaim.__table__.c.amount.type.process_result_value(claim_bound, None)
    assert restored == Decimal("10.50")


def test_withdraw_replay_with_another_amount_does_not_credit_bank(monkeypatch):
    """Не зачисляет новую сумму, если ключ уже списал другую сумму с кошелька."""

    async def _test():
        account = Account(
            id="bank_wd_conflict",
            client_id="client_1",
            balance=Decimal("500.00"),
            currency="RUB",
            status="active",
        )
        wallet = Wallet(id="wallet_wd_conflict", client_id="client_1", balance=Decimal("0.00"), status="active")
        debit = RubleTransaction(
            sender_wallet_id="wallet_wd_conflict",
            receiver_wallet_id="wallet_wd_conflict",
            amount=Decimal("100.00"),
            idempotency_key="withdraw-conflict",
            status="completed",
            response_payload=json.dumps(
                {
                    "intent": {
                        "wallet_id": "wallet_wd_conflict",
                        "bank_account_id": "bank_wd_conflict",
                        "amount": "100.00",
                    }
                }
            ),
        )
        bank = MemorySession()
        ruble = MemorySession()
        bank.rows.append(account)
        ruble.rows.extend([wallet, debit])
        monkeypatch.setattr("src.simulations.api.routers.ruble.bank_db", lambda: context(bank))
        monkeypatch.setattr("src.simulations.api.routers.ruble.ruble_db", lambda: context(ruble))
        request = FundWithdrawRequest(
            bank_account_id="bank_wd_conflict",
            amount=Decimal("1000000.00"),
            idempotency_key="withdraw-conflict",
        )

        with pytest.raises(HTTPException) as error:
            await withdraw_wallet("wallet_wd_conflict", request)

        assert error.value.status_code == 409
        assert account.balance == Decimal("500.00")
        assert wallet.balance == Decimal("0.00")

    asyncio.run(_test())


def test_close_after_conflicting_claim_does_not_pay_again(monkeypatch):
    """Закрывает вклад без второго зачисления, если этот ключ уже зачислил ту же сумму."""

    async def _test():
        deposit = Deposit(
            id="dep_paid_key",
            client_id="client_1",
            balance=Decimal("40.00"),
            status="active",
            payout_key="close-paid",
            payout_amount=Decimal("40.00"),
        )
        account = Account(
            id="bank_paid_key",
            client_id="client_1",
            balance=Decimal("140.00"),
            currency="RUB",
            status="active",
        )
        claim = BankIdempotencyClaim(
            key="close-paid",
            operation_type="eod_deposit_return",
            account_id="bank_paid_key",
            amount=Decimal("40.00"),
            direction="income",
        )
        invest = MemorySession()
        bank = MemorySession()
        invest.rows.append(deposit)
        bank.rows.extend([account, claim])
        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: context(invest))
        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: context(bank))
        request = CloseProductRequest(to_bank_account_id="bank_paid_key", idempotency_key="close-paid")

        result = await close_deposit("dep_paid_key", request)

        assert result["status"] == "success"
        assert account.balance == Decimal("140.00")
        assert deposit.status == "closed"
        assert deposit.balance == Decimal("0.00")
        assert deposit.payout_key == "close-paid"

    asyncio.run(_test())


def test_savings_eod_does_not_capitalize_during_close():
    """Не прибавляет проценты к копилке, у которой уже зафиксирована выплата."""

    async def _test():
        savings = SavingsAccount(
            id="sav_closing",
            client_id="client_1",
            balance=Decimal("100.00"),
            interest_rate=Decimal("12.00"),
            status="active",
            next_payment_date=datetime(2026, 11, 1),
            next_payment_amount=Decimal("5.00"),
            payout_key="close-sav",
            payout_amount=Decimal("100.00"),
        )
        session = AsyncMock()
        session.commit = AsyncMock()
        found = MagicMock()
        found.scalars.return_value.all.return_value = [savings]
        session.execute = AsyncMock(return_value=found)

        await process_savings_accounts(session, date(2026, 11, 5))

        assert savings.balance == Decimal("100.00")
        assert savings.next_payment_amount == Decimal("5.00")

    asyncio.run(_test())


def test_repeat_autopay_same_day_does_not_skip_a_period():
    """Повторный прогон в те же сутки не сдвигает дату автоплатежа второй раз."""

    async def _test():
        payment = AutoPayment(
            id="ap_repeat",
            account_id="bank_ap",
            amount=Decimal("10.00"),
            recipient="shop",
            schedule="monthly",
            next_payment_date=date(2026, 9, 5),
            is_active=True,
        )
        account = Account(
            id="bank_ap",
            client_id="client_1",
            balance=Decimal("100.00"),
            currency="RUB",
            status="active",
        )
        session = MemorySession()
        session.rows.extend([payment, account])
        current = date(2026, 11, 5)

        await process_auto_payments(session, current)
        await process_auto_payments(session, current)

        assert payment.next_payment_date == date(2026, 10, 5)
        assert account.balance == Decimal("90.00")
        assert len([row for row in session.rows if isinstance(row, Transaction)]) == 1

    asyncio.run(_test())


def test_transfer_rejects_key_of_another_operation(monkeypatch):
    """Не объявляет успех перевода, если ключ уже описывает другую сумму."""

    async def _test():
        sender = Account(id="sender_foreign", balance=Decimal("500.00"), status="active", currency="RUB")
        receiver = Account(id="receiver_foreign", balance=Decimal("20.00"), status="active", currency="RUB")
        record = IdempotencyRecord(
            key="transfer-foreign",
            from_account_id="sender_other",
            to_account_id="receiver_other",
            amount=Decimal("10.00"),
            transaction_id="tx_other",
            response_payload=json.dumps(
                {
                    "status": "success",
                    "transaction_id": "tx_other",
                    "signed_receipt": {
                        "signature": "sig",
                        "data": {
                            "from_account_id": "sender_other",
                            "to_account_id": "receiver_other",
                            "amount": "10.00",
                            "transfer_type": "account",
                            "destination": "receiver_other",
                        },
                    },
                }
            ),
        )
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=record)
        monkeypatch.setattr("src.simulations.api.routers.bank.get_db", lambda: context(session))
        request = TransferRequest(
            from_account_id="sender_foreign",
            to_account_id="receiver_foreign",
            amount=Decimal("300.00"),
            transfer_type="account",
            destination="receiver_foreign",
            idempotency_key="transfer-foreign",
        )

        with pytest.raises(HTTPException) as error:
            await transfer_money(request)

        assert error.value.status_code == 409
        assert sender.balance == Decimal("500.00")
        assert receiver.balance == Decimal("20.00")

    asyncio.run(_test())


def test_fund_does_not_reverse_a_withdraw_with_the_same_key(monkeypatch):
    """Не списывает кошелёк ещё раз, если ключ уже принадлежит незавершённому выводу."""

    async def _test():
        account = Account(
            id="bank_cross_key",
            client_id="client_1",
            balance=Decimal("500.00"),
            currency="RUB",
            status="active",
        )
        wallet = Wallet(id="wallet_cross_key", client_id="client_1", balance=Decimal("400.00"), status="active")
        debit = RubleTransaction(
            sender_wallet_id="wallet_cross_key",
            receiver_wallet_id="wallet_cross_key",
            amount=Decimal("100.00"),
            idempotency_key="cross-key",
            status="completed",
        )
        bank = MemorySession()
        ruble = MemorySession()
        bank.rows.append(account)
        ruble.rows.extend([wallet, debit])
        monkeypatch.setattr("src.simulations.api.routers.ruble.bank_db", lambda: context(bank))
        monkeypatch.setattr("src.simulations.api.routers.ruble.ruble_db", lambda: context(ruble))
        request = FundWithdrawRequest(
            bank_account_id="bank_cross_key",
            amount=Decimal("100.00"),
            idempotency_key="cross-key",
        )

        with pytest.raises(HTTPException) as error:
            await fund_wallet("wallet_cross_key", request)

        assert error.value.status_code == 409
        assert wallet.balance == Decimal("400.00")
        assert account.balance == Decimal("500.00")
        assert debit in ruble.rows

    asyncio.run(_test())


def test_close_adopts_unpaid_eod_payout_key(monkeypatch):
    """Закрывает вклад клиентским ключом, если ночная выплата не дошла до банка."""

    async def _test():
        deposit = Deposit(
            id="dep_eod_stuck",
            client_id="client_1",
            balance=Decimal("40.00"),
            status="active",
            payout_key="eod-mature:dep_eod_stuck",
            payout_amount=Decimal("40.00"),
        )
        account = Account(
            id="bank_eod_stuck",
            client_id="client_1",
            balance=Decimal("100.00"),
            currency="RUB",
            status="active",
        )
        invest = MemorySession()
        bank = MemorySession()
        invest.rows.append(deposit)
        bank.rows.append(account)
        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: context(invest))
        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: context(bank))
        request = CloseProductRequest(to_bank_account_id="bank_eod_stuck", idempotency_key="client-close")

        result = await close_deposit("dep_eod_stuck", request)

        assert result["status"] == "success"
        assert account.balance == Decimal("140.00")
        assert deposit.status == "closed"
        assert deposit.payout_key == "client-close"

    asyncio.run(_test())


def test_completed_withdraw_key_is_rejected_by_fund_and_vice_versa(monkeypatch):
    """Завершённый вывод не подтверждает пополнение с тем же ключом, и наоборот."""

    async def _test():
        account = Account(
            id="bank_action_check",
            client_id="client_1",
            balance=Decimal("500.00"),
            currency="RUB",
            status="active",
        )
        wallet = Wallet(id="wallet_action_check", client_id="client_1", balance=Decimal("300.00"), status="active")
        bank = MemorySession()
        ruble = MemorySession()
        bank.rows.append(account)
        ruble.rows.append(wallet)
        monkeypatch.setattr("src.simulations.api.routers.ruble.bank_db", lambda: context(bank))
        monkeypatch.setattr("src.simulations.api.routers.ruble.ruble_db", lambda: context(ruble))

        withdraw_req = FundWithdrawRequest(
            bank_account_id="bank_action_check",
            amount=Decimal("100.00"),
            idempotency_key="same-ruble-key",
        )
        await withdraw_wallet("wallet_action_check", withdraw_req)
        assert wallet.balance == Decimal("200.00")
        assert account.balance == Decimal("600.00")

        with pytest.raises(HTTPException) as fund_conflict:
            await fund_wallet("wallet_action_check", withdraw_req)
        assert fund_conflict.value.status_code == 409
        assert wallet.balance == Decimal("200.00")
        assert account.balance == Decimal("600.00")

        fund_req = FundWithdrawRequest(
            bank_account_id="bank_action_check",
            amount=Decimal("50.00"),
            idempotency_key="same-fund-key",
        )
        await fund_wallet("wallet_action_check", fund_req)
        assert wallet.balance == Decimal("250.00")
        assert account.balance == Decimal("550.00")

        with pytest.raises(HTTPException) as withdraw_conflict:
            await withdraw_wallet("wallet_action_check", fund_req)
        assert withdraw_conflict.value.status_code == 409
        assert wallet.balance == Decimal("250.00")
        assert account.balance == Decimal("550.00")

    asyncio.run(_test())


def test_smart_contract_reads_header_key_and_invest_replay_checks_params(monkeypatch):
    """Читает ключ смарт-контракта из заголовка и сверяет параметры повтора в инвестициях."""
    from fastapi import Request

    from src.simulations.api.routers.invest import open_savings
    from src.simulations.api.routers.ruble import CreateContractRequest, create_smart_contract

    async def _test():
        creator = Wallet(
            id="wallet_sc_creator",
            client_id="client_1",
            balance=Decimal("200.00"),
            frozen_balance=Decimal("0.00"),
            status="active",
        )
        ruble = MemorySession()
        ruble.rows.append(creator)
        monkeypatch.setattr("src.simulations.api.routers.ruble.ruble_db", lambda: context(ruble))
        header_request = Request(
            {
                "type": "http",
                "method": "POST",
                "url": "http://testserver/ruble/wallets/wallet_sc_creator/smart-contracts",
                "headers": [(b"x-idempotency-key", b"sc-header-key")],
            }
        )
        contract_req = CreateContractRequest(
            receiver_wallet_id="wallet_sc_receiver",
            amount=Decimal("60.00"),
            condition_type="наступление_даты",
            contract_code="release_all()",
            idempotency_key=None,
        )
        created = await create_smart_contract("wallet_sc_creator", contract_req, request=header_request)
        assert created.amount == Decimal("60.00")
        assert creator.balance == Decimal("140.00")
        assert creator.frozen_balance == Decimal("60.00")

        account = Account(
            id="bank_sav_param",
            client_id="client_1",
            balance=Decimal("500.00"),
            currency="RUB",
            status="active",
        )
        invest = MemorySession()
        bank = MemorySession()
        bank.rows.append(account)
        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: context(invest))
        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: context(bank))
        monkeypatch.setattr(
            "src.simulations.api.routers.invest.generate_unique_invest_account_number",
            AsyncMock(return_value="40820810000000000999"),
        )
        open_req = OpenProductRequest(
            client_id="client_1",
            from_bank_account_id="bank_sav_param",
            initial_amount=Decimal("100.00"),
            idempotency_key="sav-param-key",
        )
        await open_savings(open_req)
        with pytest.raises(HTTPException) as open_conflict:
            await open_savings(
                OpenProductRequest(
                    client_id="client_1",
                    from_bank_account_id="bank_sav_param",
                    initial_amount=Decimal("200.00"),
                    idempotency_key="sav-param-key",
                )
            )
        assert open_conflict.value.status_code == 409

        savings_id = str(invest.rows[-1].id)
        await close_savings(
            savings_id,
            CloseProductRequest(to_bank_account_id="bank_sav_param", idempotency_key="sav-close-param"),
        )
        with pytest.raises(HTTPException) as close_conflict:
            await close_savings(
                savings_id,
                CloseProductRequest(to_bank_account_id="bank_other", idempotency_key="sav-close-param"),
            )
        assert close_conflict.value.status_code == 409

    asyncio.run(_test())
