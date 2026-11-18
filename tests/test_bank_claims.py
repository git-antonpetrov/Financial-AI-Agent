"""Проверяет решение по банковской записи идемпотентности."""

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.simulations.core.bank_claims import (
    begin_bank_movement,
    canonical_amount,
    movement_state,
    release_bank_claim,
    remember_bank_movement,
    same_amount,
)


def test_canonical_amount_and_same_amount_normalize_kopecks():
    """Проверяет запись суммы до копеек и отказ при пустом или нечисловом значении."""
    assert canonical_amount(Decimal("10.1")) == "10.10"
    assert canonical_amount(Decimal("10.555")) == "10.56"
    assert same_amount("10.5", Decimal("10.50")) is True
    assert same_amount(None, Decimal("10.50")) is False
    assert same_amount("не-сумма", Decimal("10.50")) is False


def test_movement_state_separates_posted_absent_unknown_and_conflict():
    """Проверяет четыре состояния одной банковской записи идемпотентности."""
    posted = SimpleNamespace(
        key="k1",
        operation_type="transfer_in_ruble",
        account_id="acc",
        amount="10.50",
        direction="income",
    )
    amount = Decimal("10.50")
    assert movement_state(None, "k1", "transfer_in_ruble", "acc", amount, "income") == "absent"
    assert movement_state(posted, "k1", "transfer_in_ruble", "acc", amount, "income") == "posted"
    foreign = SimpleNamespace(key="other")
    assert movement_state(foreign, "k1", "transfer_in_ruble", "acc", amount, "income") == "unknown"
    assert movement_state(posted, "k1", "transfer_in_ruble", "acc", Decimal("11.00"), "income") == "conflict"


class _Session:
    """Держит одну строку и запоминает добавление и удаление записи."""

    def __init__(self, row=None):
        self.row = row
        self.added = []
        self.deleted = []

    async def scalar(self, _statement):
        """Возвращает заранее положенную строку."""
        return self.row

    def add(self, item) -> None:
        """Запоминает объект, который сессия должна вставить."""
        self.added.append(item)

    async def delete(self, item) -> None:
        """Запоминает объект, который сессия должна удалить."""
        self.deleted.append(item)


def _begin(session, amount: Decimal):
    """Запрашивает разрешение на одно и то же зачисление с заданной суммой."""
    return begin_bank_movement(
        session,
        key="k1",
        operation_type="transfer_in_ruble",
        account_id="acc",
        amount=amount,
        direction="income",
    )


def test_begin_bank_movement_posts_new_key_and_skips_exact_replay():
    """Проверяет, что новый ключ проводится, а точный повтор не проводится снова."""

    async def _test():
        assert await _begin(_Session(None), Decimal("10.50")) is True
        foreign = _Session(SimpleNamespace(key="other"))
        assert await _begin(foreign, Decimal("10.50")) is True
        same = _Session(
            SimpleNamespace(
                key="k1",
                operation_type="transfer_in_ruble",
                account_id="acc",
                amount="10.5",
                direction="income",
            )
        )
        assert await _begin(same, Decimal("10.50")) is False

    asyncio.run(_test())


def test_begin_bank_movement_rejects_reused_key_with_another_amount():
    """Проверяет отказ, когда тот же ключ уже занят другой суммой."""

    async def _test():
        stored = SimpleNamespace(
            key="k1",
            operation_type="transfer_in_ruble",
            account_id="acc",
            amount="10.50",
            direction="income",
        )
        with pytest.raises(HTTPException) as error:
            await _begin(_Session(stored), Decimal("99.00"))
        assert error.value.status_code == 409
        assert error.value.detail == "Ключ идемпотентности уже использован другой операцией"

    asyncio.run(_test())


def test_remember_and_release_bank_claim_keep_only_matching_key():
    """Проверяет запись канонической суммы и удаление только совпавшего ключа."""

    async def _test():
        session = _Session()
        remember_bank_movement(
            session,
            key="k1",
            operation_type="transfer_in_ruble",
            account_id="acc",
            amount=Decimal("10.1"),
            direction="income",
        )
        claim = session.added[0]
        assert claim.key == "k1"
        assert claim.amount == "10.10"
        assert claim.account_id == "acc"
        assert claim.direction == "income"
        session.row = claim
        await release_bank_claim(session, "k1")
        assert session.deleted == [claim]

        foreign = _Session(SimpleNamespace(key="other"))
        await release_bank_claim(foreign, "k1")
        assert foreign.deleted == []
        await release_bank_claim(_Session(None), "k1")

    asyncio.run(_test())
