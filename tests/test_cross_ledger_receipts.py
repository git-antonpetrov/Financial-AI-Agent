"""Проверяет порядок закрытия вклада и повтор подписанного ответа."""

import asyncio
import json
from decimal import Decimal
from unittest.mock import MagicMock

from sqlalchemy.dialects import postgresql

from src.simulations.api.routers.invest import (
    CloseProductRequest,
    OpenProductRequest,
    close_deposit,
    close_savings,
    open_broker_account,
    open_savings,
)
from src.simulations.api.routers.ruble import FundWithdrawRequest, fund_wallet, withdraw_wallet
from src.simulations.db.bank.db.models import Account, Transaction
from src.simulations.db.digital_ruble.db.models import Wallet
from src.simulations.db.invest.db.models import Deposit, SavingsAccount


def compile_pg(stmt) -> str:
    """Компилирует выражение SQLAlchemy в текст PostgreSQL."""
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def context(session):
    """Открывает переданную сессию как асинхронный контекст."""
    manager = MagicMock()
    manager.__aenter__.return_value = session
    manager.__aexit__.return_value = None
    return manager


class CatalogSession:
    """Хранит добавленные строки и отдаёт их по идентификатору или ключу из SQL."""

    def __init__(self) -> None:
        self.rows: list[object] = []

    def add(self, row: object) -> None:
        """Запоминает строку, которую код добавил в сессию."""
        self.rows.append(row)

    async def commit(self) -> None:
        """Фиксирует сессию без базы."""
        return None

    async def refresh(self, row: object) -> None:
        """Оставляет строку как есть: идентификатор уже задан кодом."""
        return None

    async def scalar(self, stmt: object) -> object | None:
        """Возвращает строку, чей идентификатор или ключ есть в тексте запроса."""
        sql = compile_pg(stmt)
        for row in self.rows:
            row_id = getattr(row, "id", None)
            key = getattr(row, "idempotency_key", None)
            close_key = getattr(row, "close_idempotency_key", None)
            if row_id and str(row_id) in sql:
                return row
            if key and str(key) in sql:
                return row
            if close_key and str(close_key) in sql:
                return row
        return None


class BankSession:
    """Списывает или зачисляет счёт один раз на ключ идемпотентности."""

    def __init__(self, account: Account) -> None:
        self.account = account
        self.transactions: list[object] = []

    def add(self, row: object) -> None:
        """Запоминает банковскую проводку."""
        self.transactions.append(row)

    async def commit(self) -> None:
        """Фиксирует сессию без базы."""
        return None

    async def scalar(self, stmt: object) -> object | None:
        """Отдаёт проводку с тем же ключом либо сам счёт."""
        sql = compile_pg(stmt)
        if "idempotency_key" in sql:
            for row in self.transactions:
                key = getattr(row, "idempotency_key", None)
                if key and str(key) in sql:
                    return row
            return None
        return self.account


def test_close_deposit_credits_bank_before_close_and_replays_receipt(monkeypatch):
    """Сначала зачисляет банк, затем закрывает вклад. Повтор отдаёт тот же чек."""

    async def _test():
        deposit = Deposit(id="dep_replay", client_id="client_1", balance=Decimal("250.50"), status="active")
        account = Account(
            id="bank_replay",
            client_id="client_1",
            balance=Decimal("10.00"),
            currency="RUB",
            status="active",
        )
        invest = CatalogSession()
        invest.rows.append(deposit)
        bank = BankSession(account)
        status_at_credit: list[str] = []

        original_add = bank.add

        def add(row: object) -> None:
            status_at_credit.append(deposit.status)
            original_add(row)

        bank.add = add
        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: context(invest))
        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: context(bank))
        request = CloseProductRequest(to_bank_account_id="bank_replay", idempotency_key="close-replay")

        first = await close_deposit("dep_replay", request)
        second = await close_deposit("dep_replay", request)

        income = [row for row in bank.transactions if isinstance(row, Transaction)]
        assert status_at_credit
        assert all(status == "active" for status in status_at_credit)
        assert deposit.status == "closed"
        assert deposit.balance == Decimal("0.00")
        assert account.balance == Decimal("260.50")
        assert len(income) == 1
        assert first["signed_receipt"] == second["signed_receipt"]
        assert second["returned_amount"] == Decimal("250.50")

    asyncio.run(_test())


def test_close_savings_credits_bank_before_close_and_replays_receipt(monkeypatch):
    """Сначала зачисляет банк, затем закрывает копилку. Повтор отдаёт тот же чек."""

    async def _test():
        savings = SavingsAccount(
            id="sav_replay",
            client_id="client_1",
            balance=Decimal("80.25"),
            status="active",
        )
        account = Account(
            id="bank_sav",
            client_id="client_1",
            balance=Decimal("5.00"),
            currency="RUB",
            status="active",
        )
        invest = CatalogSession()
        invest.rows.append(savings)
        bank = BankSession(account)
        status_at_credit: list[str] = []
        original_add = bank.add

        def add(row: object) -> None:
            status_at_credit.append(savings.status)
            original_add(row)

        bank.add = add
        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: context(invest))
        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: context(bank))
        request = CloseProductRequest(to_bank_account_id="bank_sav", idempotency_key="savings-close-replay")

        first = await close_savings("sav_replay", request)
        second = await close_savings("sav_replay", request)

        income = [row for row in bank.transactions if isinstance(row, Transaction)]
        assert status_at_credit
        assert all(status == "active" for status in status_at_credit)
        assert savings.status == "closed"
        assert savings.balance == Decimal("0.00")
        assert account.balance == Decimal("85.25")
        assert len(income) == 1
        assert first["signed_receipt"] == second["signed_receipt"]
        assert second["returned_amount"] == Decimal("80.25")

    asyncio.run(_test())


def test_product_and_wallet_replay_returns_stored_receipt(monkeypatch):
    """Повтор копилки, брокера, пополнения и вывода не создаёт вторую проводку."""

    async def _test():
        account = Account(
            id="bank_products",
            client_id="client_1",
            balance=Decimal("100000.00"),
            currency="RUB",
            status="active",
        )
        wallet = Wallet(id="wallet_products", client_id="client_1", balance=Decimal("5000.00"), status="active")
        invest = CatalogSession()
        ruble = CatalogSession()
        ruble.rows.append(wallet)
        bank = BankSession(account)
        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: context(invest))
        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: context(bank))
        monkeypatch.setattr("src.simulations.api.routers.ruble.ruble_db", lambda: context(ruble))
        monkeypatch.setattr("src.simulations.api.routers.ruble.bank_db", lambda: context(bank))

        savings_request = OpenProductRequest(
            client_id="client_1",
            from_bank_account_id="bank_products",
            initial_amount=Decimal("100.00"),
            idempotency_key="savings-replay",
        )
        first_savings = await open_savings(savings_request)
        saved_savings = json.loads(invest.rows[-1].response_payload)
        balance_after_savings = account.balance
        second_savings = await open_savings(savings_request)
        assert second_savings.account_number == first_savings.account_number
        assert json.loads(invest.rows[-1].response_payload)["signed_receipt"] == saved_savings["signed_receipt"]
        assert account.balance == balance_after_savings

        broker_request = OpenProductRequest(
            client_id="client_1",
            from_bank_account_id="bank_products",
            initial_amount=Decimal("80.00"),
            idempotency_key="broker-replay",
        )
        first_broker = await open_broker_account(broker_request)
        saved_broker = json.loads(invest.rows[-1].response_payload)
        balance_after_broker = account.balance
        second_broker = await open_broker_account(broker_request)
        assert second_broker.account_number == first_broker.account_number
        assert json.loads(invest.rows[-1].response_payload)["signed_receipt"] == saved_broker["signed_receipt"]
        assert account.balance == balance_after_broker

        fund_request = FundWithdrawRequest(
            bank_account_id="bank_products",
            amount=Decimal("30.00"),
            idempotency_key="fund-replay",
        )
        first_fund = await fund_wallet("wallet_products", fund_request)
        balance_after_fund = account.balance
        wallet_after_fund = wallet.balance
        second_fund = await fund_wallet("wallet_products", fund_request)
        assert second_fund["signed_receipt"] == first_fund["signed_receipt"]
        assert account.balance == balance_after_fund
        assert wallet.balance == wallet_after_fund

        withdraw_request = FundWithdrawRequest(
            bank_account_id="bank_products",
            amount=Decimal("20.00"),
            idempotency_key="withdraw-replay",
        )
        first_withdraw = await withdraw_wallet("wallet_products", withdraw_request)
        balance_after_withdraw = account.balance
        wallet_after_withdraw = wallet.balance
        second_withdraw = await withdraw_wallet("wallet_products", withdraw_request)
        assert second_withdraw["signed_receipt"] == first_withdraw["signed_receipt"]
        assert account.balance == balance_after_withdraw
        assert wallet.balance == wallet_after_withdraw

    asyncio.run(_test())
