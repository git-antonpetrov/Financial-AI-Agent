"""Удерживает PostgreSQL advisory lock вне транзакций EOD.

Commit сессии возвращает соединение в пул и снимает session-level lock.
Отдельное соединение остаётся занятым на всё время закрытия дня.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


def _session_engine(session: AsyncSession):
    """Возвращает движок сессии, если он есть отдельно от текущего соединения."""
    getter = getattr(session, "get_bind", None)
    if callable(getter):
        try:
            bound = getter()
        except Exception:
            bound = None
        if isinstance(bound, AsyncEngine):
            return bound
    bound = getattr(type(session), "bind", None)
    if isinstance(bound, property):
        try:
            value = session.bind
        except Exception:
            value = None
        if isinstance(value, AsyncEngine):
            return value
    return None


async def acquire_advisory_lock(session: AsyncSession, lock_id: int):
    """Захватывает pg_try_advisory_lock на соединении, которое не сдаётся в пул.

    Аргументы:
        session: Сессия рабочей нагрузки. Если у неё нет bind, lock берётся через неё.
        lock_id: Числовой идентификатор блокировки PostgreSQL.

    Возвращает:
        Пару из соединения-держателя и признака захвата. Соединение равно None,
        когда lock взят самой сессией.
    """
    bind = _session_engine(session)
    connect = getattr(bind, "connect", None) if bind is not None else None
    if connect is None:
        locked = await session.scalar(text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": lock_id})
        return None, bool(locked)
    connection = await connect()
    try:
        locked = await connection.scalar(text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": lock_id})
    except Exception:
        await connection.close()
        raise
    if not locked:
        await connection.close()
        return None, False
    return connection, True


async def release_advisory_lock(lock_connection, session: AsyncSession, lock_id: int) -> None:
    """Освобождает advisory lock и закрывает соединение, которое его держало.

    Аргументы:
        lock_connection: Соединение из acquire_advisory_lock. None, если lock держит сессия.
        session: Рабочая сессия, используется только когда отдельного соединения нет.
        lock_id: Тот же идентификатор, что был передан при захвате.
    """
    if lock_connection is not None:
        await lock_connection.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": lock_id})
        await lock_connection.commit()
        await lock_connection.close()
        return
    await session.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": lock_id})
    await session.commit()
