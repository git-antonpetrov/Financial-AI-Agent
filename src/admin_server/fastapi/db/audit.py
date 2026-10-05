"""
Модуль неизменяемого журнала аудита (Tamper-evident Hash-chained Audit Log).
Реализует криптографическую связку записей (hash-chaining) по аналогии с блокчейном:
каждая новая запись включает хэш предыдущей (prev_hash), делая ретроспективную
модификацию или удаление записей математически обнаруживаемыми.
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, text
from . import models

from core.utils.console_logger import log_warning, log_error

GENESIS_HASH = "0000000000000000000000000000000000000000000000000000000000000000"


def format_audit_timestamp(dt: datetime) -> str:
    """Приводит дату и время к строгому формату UTC ISO 8601 для воспроизводимого хеширования."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def compute_audit_hash(
    timestamp_str: str,
    actor: str,
    actor_ip: Optional[str],
    action: str,
    resource: Optional[str],
    status: str,
    details: Optional[str],
    prev_hash: str
) -> str:
    """
    Вычисляет криптографический SHA-256 хэш записи аудита на основе канонического строкового представления.
    Формат: {timestamp}|{actor}|{actor_ip}|{action}|{resource}|{status}|{details}|{prev_hash}
    """
    payload = "|".join([
        timestamp_str.strip(),
        actor.strip(),
        (actor_ip or "").strip(),
        action.strip(),
        (resource or "").strip(),
        status.strip(),
        (details or "").strip(),
        prev_hash.strip()
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def log_audit_event(
    db: Optional[AsyncSession],
    actor: str,
    action: str,
    status: str = "SUCCESS",
    resource: Optional[str] = None,
    actor_ip: Optional[str] = None,
    details: Optional[Any] = None,
    timestamp: Optional[datetime] = None
) -> Optional[models.AuditLog]:
    """
    Атомарно создает новую запись в криптографически связанном журнале аудита.
    Автоматически извлекает хэш последней записи и формирует неразрывную цепочку.
    """
    if db is None:
        return None

    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    # Приведение details к строковому формату (JSON при словаре/списке)
    details_str: Optional[str] = None
    if details is not None:
        if isinstance(details, (dict, list)):
            details_str = json.dumps(details, ensure_ascii=False, sort_keys=True)
        else:
            details_str = str(details)

    # Защита от состояния гонки при параллельных транзакциях в PostgreSQL
    try:
        bind = getattr(db, "get_bind", lambda: None)() if hasattr(db, "get_bind") else None
        if bind and getattr(bind, "dialect", None) and getattr(bind.dialect, "name", "") == "postgresql":
            # Константный advisory lock для строгой последовательности цепочки аудита
            await db.execute(text("SELECT pg_advisory_xact_lock(987654321)"))
    except Exception:
        pass

    # Получение последней записи в журнале
    prev_hash = GENESIS_HASH
    try:
        query_last = select(models.AuditLog).order_by(models.AuditLog.id.desc()).limit(1)
        res = await db.execute(query_last)
        last_log = res.scalars().first() if hasattr(res, "scalars") else None
        if last_log and getattr(last_log, "record_hash", None):
            prev_hash = last_log.record_hash
    except Exception as e:
        log_warning("Audit", f"Не удалось извлечь последнюю запись аудита (fallback): {e}")

    ts_str = format_audit_timestamp(timestamp)
    record_hash = compute_audit_hash(
        timestamp_str=ts_str,
        actor=actor,
        actor_ip=actor_ip,
        action=action,
        resource=resource,
        status=status,
        details=details_str,
        prev_hash=prev_hash
    )

    audit_entry = models.AuditLog(
        timestamp=timestamp,
        actor=actor,
        actor_ip=actor_ip,
        action=action,
        resource=resource,
        status=status,
        details=details_str,
        prev_hash=prev_hash,
        record_hash=record_hash
    )

    try:
        db.add(audit_entry)
        await db.commit()
        await db.refresh(audit_entry)
    except Exception as e:
        log_warning("Audit", f"Не удалось сохранить запись аудита (fallback): {e}")

    return audit_entry


async def verify_audit_chain(db: AsyncSession) -> dict[str, Any]:
    """
    Проверяет криптографическую целостность всей цепочки журнала аудита (Tamper Verification).
    Выявляет подделки данных внутри записей, удаление строк или ретроспективную вставку.
    """
    query = select(models.AuditLog).order_by(models.AuditLog.id.asc())
    res = await db.execute(query)
    entries = res.scalars().all()

    if not entries:
        return {
            "is_valid": True,
            "total_records": 0,
            "broken_record_id": None,
            "details": "Журнал аудита пуст, цепочка не содержит записей."
        }

    expected_prev_hash = GENESIS_HASH

    for idx, entry in enumerate(entries):
        # 1. Проверка связности с предыдущей записью
        if entry.prev_hash != expected_prev_hash:
            return {
                "is_valid": False,
                "total_records": len(entries),
                "broken_record_id": entry.id,
                "broken_record_index": idx,
                "details": (
                    f"Нарушение целостности цепочки (разрыв связи) на записи id={entry.id}: "
                    f"ожидался prev_hash='{expected_prev_hash}', фактически='{entry.prev_hash}'"
                )
            }

        # 2. Проверка неизменности самой записи (пересчет хэша)
        ts_str = format_audit_timestamp(entry.timestamp)
        recomputed_hash = compute_audit_hash(
            timestamp_str=ts_str,
            actor=entry.actor,
            actor_ip=entry.actor_ip,
            action=entry.action,
            resource=entry.resource,
            status=entry.status,
            details=entry.details,
            prev_hash=entry.prev_hash
        )

        if entry.record_hash != recomputed_hash:
            return {
                "is_valid": False,
                "total_records": len(entries),
                "broken_record_id": entry.id,
                "broken_record_index": idx,
                "details": (
                    f"Обнаружена подделка данных (Tamper detected) на записи id={entry.id}: "
                    f"хэш в базе='{entry.record_hash}', вычисленный='{recomputed_hash}'"
                )
            }

        expected_prev_hash = entry.record_hash

    return {
        "is_valid": True,
        "total_records": len(entries),
        "broken_record_id": None,
        "details": f"Криптографическая цепочка полностью подтверждена: проверено {len(entries)} записей."
    }


async def get_audit_logs(
    db: AsyncSession,
    limit: int = 50,
    offset: int = 0,
    actor: Optional[str] = None,
    action: Optional[str] = None,
    status: Optional[str] = None
) -> tuple[list[models.AuditLog], int]:
    """Возвращает список записей журнала аудита с фильтрацией и постраничной навигацией."""
    count_stmt = select(func.count(models.AuditLog.id))
    query = select(models.AuditLog)

    if actor:
        count_stmt = count_stmt.where(models.AuditLog.actor == actor)
        query = query.where(models.AuditLog.actor == actor)
    if action:
        count_stmt = count_stmt.where(models.AuditLog.action == action)
        query = query.where(models.AuditLog.action == action)
    if status:
        count_stmt = count_stmt.where(models.AuditLog.status == status)
        query = query.where(models.AuditLog.status == status)

    total_count_res = await db.execute(count_stmt)
    total_count = total_count_res.scalar_one()

    query = query.order_by(models.AuditLog.id.desc()).offset(offset).limit(limit)
    res = await db.execute(query)
    items = res.scalars().all()

    return list(items), total_count


async def get_audit_summary(db: AsyncSession) -> dict[str, Any]:
    """Возвращает агрегированную статистику журнала аудита и статус целостности цепочки."""
    total_res = await db.execute(select(func.count(models.AuditLog.id)))
    total = total_res.scalar_one()

    success_res = await db.execute(
        select(func.count(models.AuditLog.id)).where(models.AuditLog.status == "SUCCESS")
    )
    success_count = success_res.scalar_one()

    failure_res = await db.execute(
        select(func.count(models.AuditLog.id)).where(models.AuditLog.status == "FAILURE")
    )
    failure_count = failure_res.scalar_one()

    warning_res = await db.execute(
        select(func.count(models.AuditLog.id)).where(models.AuditLog.status == "WARNING")
    )
    warning_count = warning_res.scalar_one()

    last_res = await db.execute(
        select(models.AuditLog).order_by(models.AuditLog.id.desc()).limit(1)
    )
    last_log = last_res.scalars().first()

    verification = await verify_audit_chain(db)

    return {
        "total_records": total,
        "success_count": success_count,
        "failure_count": failure_count,
        "warning_count": warning_count,
        "last_event": last_log,
        "is_chain_intact": verification["is_valid"],
        "verification_details": verification["details"]
    }
