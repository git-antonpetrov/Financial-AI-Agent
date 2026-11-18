"""Проверяет зарплату и абонентскую плату: одно начисление и отказ без денег или даты."""

import asyncio
import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

from sqlalchemy.dialects import postgresql

from src.simulations.db.bank.db.models import Account, Tariff, Transaction
from src.simulations.workers.bank_eod.tasks import process_salaries, process_tariff_fees

IVANOV = "11111111-1111-1111-1111-111111111111"
PAYDAY = date(2026, 11, 10)
FEE_DAY = date(2026, 11, 1)


def _sql(stmt) -> str:
    """Компилирует запрос воркера в текст PostgreSQL с подставленными значениями."""
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


class _PayrollSession:
    """Отдаёт активные счета и уже проведённые зарплаты и тарифные платы."""

    def __init__(self, accounts: list[Account]) -> None:
        self.accounts = accounts
        self.transactions: list[Transaction] = []
        self.commits = 0

    def add(self, row: object) -> None:
        """Запоминает проводку и назначает идентификатор, как это делает flush."""
        if isinstance(row, Transaction):
            if row.id is None:
                row.id = str(uuid.uuid4())
            self.transactions.append(row)

    async def commit(self) -> None:
        """Считает фиксации сессии без базы."""
        self.commits += 1

    async def execute(self, stmt):
        """Возвращает счета или идентификатор уже завершённой проводки."""
        sql = _sql(stmt)
        result = MagicMock()
        if "transactions" in sql and "salary" in sql:
            found = self._completed("salary", sql, by_client=True)
            result.scalar_one_or_none.return_value = found[0].id if found else None
            return result
        if "transactions" in sql and "service_fee" in sql:
            found = self._completed("service_fee", sql, by_client=False)
            result.scalar_one_or_none.return_value = found[0].id if found else None
            return result
        result.scalars.return_value.all.return_value = [
            account for account in self.accounts if account.status == "active"
        ]
        return result

    def _completed(self, operation: str, sql: str, by_client: bool) -> list[Transaction]:
        """Оставляет проведённые операции, которые запрос воркера действительно ищет."""
        found = []
        for tx in self.transactions:
            if tx.operation_type != operation or tx.status != "completed":
                continue
            if str(tx.date) not in sql:
                continue
            owner = next(account for account in self.accounts if account.id == tx.account_id)
            marker = owner.client_id if by_client else tx.account_id
            if marker not in sql:
                continue
            found.append(tx)
        return found


class _FlakyFeeSession(_PayrollSession):
    """Роняет первую проверку платы и пропускает остальные счета дальше."""

    def __init__(self, accounts: list[Account]) -> None:
        super().__init__(accounts)
        self.failures_left = 1

    async def execute(self, stmt):
        """Поднимает ошибку на первой проверке service_fee."""
        sql = _sql(stmt)
        if "service_fee" in sql and self.failures_left:
            self.failures_left -= 1
            raise RuntimeError("проверка платы сорвалась")
        return await super().execute(stmt)


def _account(account_id: str, client_id: str, balance: str, tariff: object | None = None) -> Account:
    """Собирает активный счёт с остатком и тарифом в памяти."""
    account = Account(id=account_id, client_id=client_id, balance=Decimal(balance), status="active")
    account.tariff = tariff
    return account


def _patch_salary(monkeypatch) -> None:
    """Фиксирует сумму зарплаты, чтобы проверка не зависела от random."""
    monkeypatch.setattr(
        "src.simulations.workers.bank_eod.tasks.random.randint",
        lambda _low, _high: 90000,
    )


def test_salary_is_credited_once_on_payday(monkeypatch):
    """Начисляет зарплату известному клиенту в день выплаты и не повторяет её."""

    async def _test():
        _patch_salary(monkeypatch)
        account = _account("acc-ivan", IVANOV, "10.00")
        session = _PayrollSession([account])

        await process_salaries(session, PAYDAY)
        await process_salaries(session, PAYDAY)

        assert account.balance == Decimal("90010.00")
        assert session.commits == 2
        salaries = [tx for tx in session.transactions if tx.operation_type == "salary"]
        assert len(salaries) == 1
        assert salaries[0].account_id == "acc-ivan"
        assert salaries[0].amount == Decimal("90000")
        assert salaries[0].category == "income"
        assert salaries[0].status == "completed"
        assert salaries[0].date == PAYDAY

    asyncio.run(_test())


def test_salary_credits_only_one_account_of_client(monkeypatch):
    """Начисляет зарплату на первый активный счёт клиента и не трогает второй."""

    async def _test():
        _patch_salary(monkeypatch)
        first = _account("acc-first", IVANOV, "1.00")
        second = _account("acc-second", IVANOV, "2.00")
        session = _PayrollSession([first, second])

        await process_salaries(session, PAYDAY)

        assert first.balance == Decimal("90001.00")
        assert second.balance == Decimal("2.00")
        assert len(session.transactions) == 1
        assert session.transactions[0].account_id == "acc-first"

    asyncio.run(_test())


def test_salary_tariff_credits_client_outside_named_list(monkeypatch):
    """Начисляет зарплату клиенту с тарифом «Зарплатный», даже если его нет в списке имён."""

    async def _test():
        _patch_salary(monkeypatch)
        tariff = Tariff(id="tariff-salary", name="Зарплатный", service_cost=Decimal("0.00"))
        account = _account("acc-payroll", "44444444-4444-4444-4444-444444444444", "5.00", tariff)
        session = _PayrollSession([account])

        await process_salaries(session, date(2026, 11, 25))

        assert account.balance == Decimal("90005.00")
        assert len(session.transactions) == 1
        assert session.transactions[0].operation_type == "salary"

    asyncio.run(_test())


def test_salary_skips_other_days_and_ineligible_clients(monkeypatch):
    """Не начисляет зарплату в обычный день и клиенту без зарплатного тарифа."""

    async def _test():
        _patch_salary(monkeypatch)
        basic = Tariff(id="tariff-basic", name="Базовый", service_cost=Decimal("0.00"))
        account = _account("acc-basic", "55555555-5555-5555-5555-555555555555", "70.00", basic)
        session = _PayrollSession([account])

        await process_salaries(session, date(2026, 11, 11))
        await process_salaries(session, PAYDAY)

        assert account.balance == Decimal("70.00")
        assert session.transactions == []
        assert session.commits == 1

    asyncio.run(_test())


def test_tariff_fee_debits_once_and_refuses_short_balance():
    """Списывает плату один раз. Нехватка денег не уменьшает остаток и не закрывает повтор."""

    async def _test():
        rich_tariff = Tariff(id="tariff-rich", name="Премиум", service_cost=Decimal("50.50"))
        poor_tariff = Tariff(id="tariff-poor", name="Базовый", service_cost=Decimal("80.00"))
        rich = _account("acc-rich", "client-rich", "200.00", rich_tariff)
        poor = _account("acc-poor", "client-poor", "40.00", poor_tariff)
        session = _PayrollSession([rich, poor])

        await process_tariff_fees(session, FEE_DAY)
        await process_tariff_fees(session, FEE_DAY)
        await process_tariff_fees(session, date(2026, 11, 2))

        assert rich.balance == Decimal("149.50")
        assert poor.balance == Decimal("40.00")
        completed = [tx for tx in session.transactions if tx.status == "completed"]
        failed = [tx for tx in session.transactions if tx.status == "failed"]
        assert len(completed) == 1
        assert completed[0].account_id == "acc-rich"
        assert completed[0].operation_type == "service_fee"
        assert completed[0].amount == Decimal("50.50")
        assert len(failed) == 2
        assert {tx.account_id for tx in failed} == {"acc-poor"}
        assert all(tx.amount == Decimal("80.00") for tx in failed)
        assert all("Недостаточно средств" in tx.description for tx in failed)
        assert session.commits == 2

    asyncio.run(_test())


def test_tariff_fee_continues_after_one_account_fails():
    """Списывает плату со следующего счёта, если чтение тарифа предыдущего упало."""

    async def _test():
        broken_tariff = Tariff(id="tariff-broken", name="Премиум", service_cost=Decimal("15.00"))
        healthy_tariff = Tariff(id="tariff-ok", name="Базовый", service_cost=Decimal("10.00"))
        broken = _account("acc-broken", "client-broken", "300.00", broken_tariff)
        healthy = _account("acc-healthy", "client-healthy", "30.00", healthy_tariff)
        session = _FlakyFeeSession([broken, healthy])

        await process_tariff_fees(session, FEE_DAY)

        assert broken.balance == Decimal("300.00")
        assert healthy.balance == Decimal("20.00")
        assert len(session.transactions) == 1
        assert session.transactions[0].account_id == "acc-healthy"
        assert session.transactions[0].status == "completed"

    asyncio.run(_test())
