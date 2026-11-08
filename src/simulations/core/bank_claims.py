"""Решает, можно ли выполнить банковское движение по ключу идемпотентности."""

from decimal import Decimal, InvalidOperation

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


def same_amount(stored: object, amount: Decimal) -> bool:
    """Сравнивает сохранённую сумму с суммой запроса после нормализации до копеек.

    Аргументы:
        stored: Значение из записи. Это строка или Decimal.
        amount: Сумма текущего запроса.

    Возвращает:
        True, если обе суммы задают одну и ту же величину.
    """
    if stored is None:
        return False
    try:
        return canonical_amount(Decimal(str(stored))) == canonical_amount(amount)
    except (InvalidOperation, ValueError):
        return False


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
        and same_amount(getattr(row, "amount", None), amount)
        and getattr(row, "direction", None) == direction
    )


def movement_state(
    row: object | None,
    key: str,
    operation_type: str,
    account_id: str,
    amount: Decimal,
    direction: str,
) -> str:
    """Определяет, зафиксировано ли банковское движение с этим ключом.

    Аргументы:
        row: Найденная запись, пустое значение или чужой объект сессии.
        key: Ключ идемпотентности.
        operation_type: Тип операции.
        account_id: Счёт движения.
        amount: Сумма.
        direction: ``expense`` или ``income``.

    Возвращает:
        ``posted``, если это же движение уже зафиксировано. ``absent``, если записи нет.
        ``unknown``, если сессия вернула объект другого вида. ``conflict``, если ключ занят другой операцией.
    """
    if row is None:
        return "absent"
    if getattr(row, "key", None) != key:
        return "unknown"
    if claim_matches(row, key, operation_type, account_id, amount, direction):
        return "posted"
    return "conflict"


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
