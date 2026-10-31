"""Решает, можно ли выполнить банковское движение по ключу идемпотентности."""

from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.future import select

from src.simulations.db.bank.db.models import BankIdempotencyClaim


def canonical_amount(amount: Decimal) -> str:
    """Возвращает сумму в виде строки с двумя знаками после запятой.

    Аргументы:
        amount: Денежная сумма операции.

    Возвращает:
        Каноническую запись суммы.
    """
    return format(Decimal(str(amount)).quantize(Decimal("0.01")), "f")


def claim_matches(row: object, key: str, operation_type: str, account_id: str, amount: Decimal, direction: str) -> bool:
    """Проверяет, что запись описывает то же движение.

    Аргументы:
        row: Найденная запись или чужой объект сессии.
        key: Ключ идемпотентности.
        operation_type: Тип операции.
        account_id: Счёт движения.
        amount: Сумма.
        direction: Направление, ``expense`` или ``income``.

    Возвращает:
        True, если ключ и отпечаток операции совпадают.
    """
    if getattr(row, "key", None) != key:
        return False
    return (
        getattr(row, "operation_type", None) == operation_type
        and str(getattr(row, "account_id", "")) == str(account_id)
        and str(getattr(row, "amount", "")) == canonical_amount(amount)
        and getattr(row, "direction", None) == direction
    )


async def begin_bank_movement(
    session,
    *,
    key: str,
    operation_type: str,
    account_id: str,
    amount: Decimal,
    direction: str,
) -> bool:
    """Разрешает движение либо сообщает, что оно уже выполнено.

    Аргументы:
        session: Открытая банковская сессия.
        key: Ключ идемпотентности.
        operation_type: Тип операции.
        account_id: Счёт движения.
        amount: Сумма.
        direction: ``expense`` или ``income``.

    Возвращает:
        True, если движение нужно выполнить. False, если это точный повтор.

    Вызывает:
        HTTPException: Ключ уже принадлежит другой операции.
    """
    existing = await session.scalar(select(BankIdempotencyClaim).where(BankIdempotencyClaim.key == key))
    if getattr(existing, "key", None) != key:
        return True
    if claim_matches(existing, key, operation_type, account_id, amount, direction):
        return False
    raise HTTPException(status_code=409, detail="Ключ идемпотентности уже использован другой операцией")


def remember_bank_movement(
    session,
    *,
    key: str,
    operation_type: str,
    account_id: str,
    amount: Decimal,
    direction: str,
) -> None:
    """Добавляет запись идемпотентности в текущую сессию.

    Аргументы:
        session: Сессия, в которой уже изменена проводка.
        key: Ключ идемпотентности.
        operation_type: Тип операции.
        account_id: Счёт движения.
        amount: Сумма.
        direction: ``expense`` или ``income``.

    Побочные эффекты:
        Ставит запись в сессию. Фиксацию выполняет вызывающий код.
    """
    session.add(
        BankIdempotencyClaim(
            key=key,
            operation_type=operation_type,
            account_id=str(account_id),
            amount=canonical_amount(amount),
            direction=direction,
        )
    )


async def release_bank_claim(session, key: str) -> None:
    """Удаляет запись идемпотентности, если откат снял движение.

    Аргументы:
        session: Банковская сессия отката.
        key: Ключ исходной операции.

    Побочные эффекты:
        Удаляет запись с этим ключом. Чужой объект сессии не удаляет.
    """
    existing = await session.scalar(select(BankIdempotencyClaim).where(BankIdempotencyClaim.key == key))
    if getattr(existing, "key", None) == key:
        await session.delete(existing)
