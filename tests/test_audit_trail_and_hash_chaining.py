"""
Модульные и интеграционные тесты защищенного журнала аудита (Audit Trail / Hash-chaining / Tamper-evident Logging):
1. Детерминированное вычисление канонического хэша записи SHA-256;
2. Связывание записей в криптографическую цепочку (prev_hash -> record_hash);
3. Верификация валидной цепочки (verify_audit_chain);
4. Обнаружение подделки данных (tamper detection) при изменении содержимого записи;
5. Обнаружение разрыва цепочки при изменении связующего prev_hash;
6. Обнаружение несанкционированного удаления записи из середины цепочки;
7. Фильтрация и пагинация журнала аудита (get_audit_logs);
8. Агрегированная сводка аудита (get_audit_summary);
9. Защита API эндпоинтов аудита (401 Unauthorized без JWT);
10. Работа HTTP эндпоинтов /api/audit/logs, /api/audit/verify, /api/audit/summary;
11. Фиксация событий входа и попыток подбора в эндпоинте /login.
"""

import os
import sys
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
import pytest

# Предварительный импорт fastapi до модификаций sys.path
import fastapi
from fastapi.testclient import TestClient

# Моки внешних сервисов
for mod in ["redis", "minio", "litellm", "asyncpg"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

# Настройка путей
sys.path.insert(0, os.path.abspath("src/admin_server"))
sys.path.insert(0, os.path.abspath("src/admin_server/fastapi"))

import security
from security import (
    settings,
    create_access_token,
    get_password_hash,
)
import server
from server import app
from db import models
from db import audit
from db.audit import (
    GENESIS_HASH,
    compute_audit_hash,
    format_audit_timestamp,
    log_audit_event,
    verify_audit_chain,
    get_audit_logs,
    get_audit_summary,
)
from db.database import get_db


# ------------------------------------------------------------------------------
# Вспомогательный Mock сессии БД и Redis для изолированного тестирования
# ------------------------------------------------------------------------------

class InMemoryRedisMock:
    """Имитатор Redis для тестирования черного списка и ротации токенов в памяти."""
    def __init__(self):
        self.store = {}

    def exists(self, key: str) -> bool:
        return key in self.store

    def setex(self, key: str, ttl: int, value: str):
        self.store[key] = value

    def get(self, key: str):
        return self.store.get(key)

    def delete(self, key: str):
        self.store.pop(key, None)

    def incr(self, key: str):
        val = int(self.store.get(key, 0)) + 1
        self.store[key] = str(val)
        return val

    def expire(self, key: str, ttl: int):
        pass


@pytest.fixture(autouse=True)
def setup_mock_redis(monkeypatch):
    """Изолирует хранилище Redis для каждого теста."""
    mock_redis = InMemoryRedisMock()
    monkeypatch.setattr(security, "redis_blacklist", mock_redis)
    monkeypatch.setattr(server, "redis_client", mock_redis)
    return mock_redis


class MockAuditSession:
    """Имитатор AsyncSession для моделей AuditLog в памяти."""
    def __init__(self):
        self.logs: list[models.AuditLog] = []

    def get_bind(self):
        return None

    def add(self, entry: models.AuditLog):
        if not hasattr(entry, "id") or entry.id is None:
            entry.id = len(self.logs) + 1
        if entry not in self.logs:
            self.logs.append(entry)

    async def commit(self):
        pass

    async def refresh(self, entry: models.AuditLog):
        pass

    async def delete(self, entry: models.AuditLog):
        if entry in self.logs:
            self.logs.remove(entry)

    async def execute(self, statement):
        import re
        try:
            sql_text = str(statement.compile(compile_kwargs={"literal_binds": True}))
        except Exception:
            sql_text = str(statement)

        sql_lower = sql_text.lower()
        items = list(self.logs)

        # Фильтрация по полям модели из скомпилированного SQL
        actor_match = re.search(r"audit_logs\.actor\s*=\s*'([^']+)'", sql_text, re.IGNORECASE)
        if actor_match:
            items = [x for x in items if x.actor == actor_match.group(1)]

        action_match = re.search(r"audit_logs\.action\s*=\s*'([^']+)'", sql_text, re.IGNORECASE)
        if action_match:
            items = [x for x in items if x.action == action_match.group(1)]

        status_match = re.search(r"audit_logs\.status\s*=\s*'([^']+)'", sql_text, re.IGNORECASE)
        if status_match:
            items = [x for x in items if x.status == status_match.group(1)]

        # 1. Запросы COUNT
        if "count" in sql_lower:
            count_val = len(items)

            class CountResult:
                def __init__(self, val):
                    self._val = val
                def scalar_one(self):
                    return self._val
                def scalar(self):
                    return self._val

            return CountResult(count_val)

        # 2. Сортировка
        if "order by" in sql_lower:
            if "id desc" in sql_lower or "desc" in sql_lower:
                items.sort(key=lambda x: x.id, reverse=True)
            else:
                items.sort(key=lambda x: x.id, reverse=False)

        # 3. Пагинация limit / offset
        offset_val = getattr(statement, "_offset", None)
        limit_val = getattr(statement, "_limit", None)
        if offset_val is not None:
            items = items[offset_val:]
        if limit_val is not None:
            items = items[:limit_val]

        class ScalarResult:
            def __init__(self, res_list):
                self._list = res_list
            def scalars(self):
                return self
            def all(self):
                return list(self._list)
            def first(self):
                return self._list[0] if self._list else None

        return ScalarResult(items)


# ------------------------------------------------------------------------------
# 1. Модульные тесты канонического хэширования
# ------------------------------------------------------------------------------

def test_compute_audit_hash_deterministic():
    """Проверяет, что вычисление SHA-256 хэша строго детерминировано."""
    ts = "2026-10-05T01:00:00+00:00"
    h1 = compute_audit_hash(
        timestamp_str=ts,
        actor="admin",
        actor_ip="127.0.0.1",
        action="AUTH_LOGIN_SUCCESS",
        resource=None,
        status="SUCCESS",
        details="User logged in",
        prev_hash=GENESIS_HASH
    )
    h2 = compute_audit_hash(
        timestamp_str=ts,
        actor="admin",
        actor_ip="127.0.0.1",
        action="AUTH_LOGIN_SUCCESS",
        resource=None,
        status="SUCCESS",
        details="User logged in",
        prev_hash=GENESIS_HASH
    )
    assert len(h1) == 64
    assert h1 == h2


def test_compute_audit_hash_sensitive_to_any_change():
    """Проверяет, что изменение любого параметра меняет итоговый хэш."""
    ts = "2026-10-05T01:00:00+00:00"
    base_hash = compute_audit_hash(
        timestamp_str=ts,
        actor="admin",
        actor_ip="127.0.0.1",
        action="AUTH_LOGIN_SUCCESS",
        resource=None,
        status="SUCCESS",
        details="Context",
        prev_hash=GENESIS_HASH
    )

    # Изменение actor
    assert base_hash != compute_audit_hash(
        timestamp_str=ts, actor="hacker", actor_ip="127.0.0.1", action="AUTH_LOGIN_SUCCESS",
        resource=None, status="SUCCESS", details="Context", prev_hash=GENESIS_HASH
    )
    # Изменение action
    assert base_hash != compute_audit_hash(
        timestamp_str=ts, actor="admin", actor_ip="127.0.0.1", action="AUTH_LOGIN_FAILED",
        resource=None, status="SUCCESS", details="Context", prev_hash=GENESIS_HASH
    )
    # Изменение prev_hash
    assert base_hash != compute_audit_hash(
        timestamp_str=ts, actor="admin", actor_ip="127.0.0.1", action="AUTH_LOGIN_SUCCESS",
        resource=None, status="SUCCESS", details="Context", prev_hash="f" * 64
    )


# ------------------------------------------------------------------------------
# 2. Модульные тесты построения цепочки аудита (Hash Chaining)
# ------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_audit_log_event_creation_and_chaining():
    """Проверяет создание записей и связывание каждой записи с хэшем предыдущей."""
    session = MockAuditSession()

    # Запись 1 (Genesis)
    log1 = await log_audit_event(
        db=session,
        actor="admin",
        action="AUTH_LOGIN_SUCCESS",
        status="SUCCESS",
        actor_ip="192.168.1.100"
    )
    assert log1.id == 1
    assert log1.prev_hash == GENESIS_HASH
    assert len(log1.record_hash) == 64

    # Запись 2
    log2 = await log_audit_event(
        db=session,
        actor="admin",
        action="KEY_ROTATED",
        status="SUCCESS",
        resource="agent:bank",
        details={"kid": "bank_202610"}
    )
    assert log2.id == 2
    assert log2.prev_hash == log1.record_hash

    # Запись 3
    log3 = await log_audit_event(
        db=session,
        actor="agent:bank",
        action="DOC_UPLOADED",
        status="SUCCESS",
        resource="doc:hash999"
    )
    assert log3.id == 3
    assert log3.prev_hash == log2.record_hash


# ------------------------------------------------------------------------------
# 3. Модульные тесты верификации цепочки и обнаружения подделок (Tamper Detection)
# ------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_verify_audit_chain_valid():
    """Проверяет успешную верификацию неповрежденной цепочки аудита."""
    session = MockAuditSession()
    for i in range(5):
        await log_audit_event(
            db=session,
            actor=f"user_{i}",
            action=f"ACTION_{i}",
            status="SUCCESS",
            details=f"Event number {i}"
        )

    res = await verify_audit_chain(session)
    assert res["is_valid"] is True
    assert res["total_records"] == 5
    assert res["broken_record_id"] is None


@pytest.mark.anyio
async def test_tamper_detection_modified_content():
    """Проверяет обнаружение подделки данных внутри существующей записи."""
    session = MockAuditSession()
    for i in range(3):
        await log_audit_event(
            db=session,
            actor="admin",
            action=f"ACTION_{i}",
            status="SUCCESS"
        )

    # Злоумышленник изменил поле action во 2-й записи без обновления record_hash
    session.logs[1].action = "MALICIOUS_TAMPERED_ACTION"

    res = await verify_audit_chain(session)
    assert res["is_valid"] is False
    assert res["broken_record_id"] == session.logs[1].id
    assert "Tamper detected" in res["details"] or "подделка" in res["details"].lower()


@pytest.mark.anyio
async def test_tamper_detection_broken_chain_link():
    """Проверяет обнаружение разрыва связующего хэша prev_hash."""
    session = MockAuditSession()
    for i in range(4):
        await log_audit_event(
            db=session,
            actor="admin",
            action=f"ACTION_{i}"
        )

    # Злоумышленник изменил prev_hash 3-й записи
    session.logs[2].prev_hash = "a" * 64

    res = await verify_audit_chain(session)
    assert res["is_valid"] is False
    assert res["broken_record_id"] == session.logs[2].id
    assert "разрыв" in res["details"].lower()


@pytest.mark.anyio
async def test_tamper_detection_deleted_middle_record():
    """Проверяет обнаружение удаления записи из середины цепочки."""
    session = MockAuditSession()
    for i in range(3):
        await log_audit_event(
            db=session,
            actor="admin",
            action=f"ACTION_{i}"
        )

    deleted_id = session.logs[1].id
    session.logs.pop(1)  # удаляем среднюю запись

    res = await verify_audit_chain(session)
    assert res["is_valid"] is False
    # Запись 3 теперь не совпадает с record_hash записи 1
    assert res["broken_record_id"] == session.logs[1].id
    assert "разрыв" in res["details"].lower()


# ------------------------------------------------------------------------------
# 4. Модульные тесты выборки и агрегированной сводки
# ------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_audit_logs_pagination_and_filters():
    """Проверяет фильтрацию и постраничную навигацию по журналу."""
    session = MockAuditSession()
    await log_audit_event(session, actor="admin", action="LOGIN", status="SUCCESS")
    await log_audit_event(session, actor="admin", action="KEY_ROTATED", status="SUCCESS")
    await log_audit_event(session, actor="operator", action="LOGIN", status="FAILURE")
    await log_audit_event(session, actor="operator", action="LOGOUT", status="SUCCESS")

    # 1. Фильтр по actor
    logs, total = await get_audit_logs(session, actor="admin")
    assert total == 2
    assert all(l.actor == "admin" for l in logs)

    # 2. Фильтр по status
    logs_fail, total_fail = await get_audit_logs(session, status="FAILURE")
    assert total_fail == 1
    assert logs_fail[0].action == "LOGIN"

    # 3. Пагинация limit / offset
    logs_page, _ = await get_audit_logs(session, limit=2, offset=1)
    assert len(logs_page) == 2


@pytest.mark.anyio
async def test_get_audit_summary():
    """Проверяет расчет сводной статистики по журналу аудита."""
    session = MockAuditSession()
    await log_audit_event(session, actor="admin", action="A1", status="SUCCESS")
    await log_audit_event(session, actor="admin", action="A2", status="SUCCESS")
    await log_audit_event(session, actor="admin", action="A3", status="FAILURE")
    await log_audit_event(session, actor="admin", action="A4", status="WARNING")

    summary = await get_audit_summary(session)
    assert summary["total_records"] == 4
    assert summary["success_count"] == 2
    assert summary["failure_count"] == 1
    assert summary["warning_count"] == 1
    assert summary["is_chain_intact"] is True
    assert summary["last_event"].action == "A4"


# ------------------------------------------------------------------------------
# 5. Интеграционные тесты API эндпоинтов аудита
# ------------------------------------------------------------------------------

def test_api_audit_endpoints_unauthorized():
    """Проверяет запрет доступа к API аудита без аутентификации (401 Unauthorized)."""
    client = TestClient(app)

    r1 = client.get("/api/audit/logs")
    assert r1.status_code == 401

    r2 = client.get("/api/audit/verify")
    assert r2.status_code == 401

    r3 = client.get("/api/audit/summary")
    assert r3.status_code == 401


def test_api_audit_endpoints_authorized():
    """Проверяет успешное обращение к API аудита с валидным JWT-токеном."""
    mock_session = MockAuditSession()

    # Переопределяем get_db
    app.dependency_overrides[get_db] = lambda: mock_session

    try:
        # Создаем тестовые записи
        token = create_access_token({"sub": "admin"})
        client = TestClient(app)
        headers = {"Authorization": f"Bearer {token}"}

        # 1. Получение сводки (summary)
        resp_sum = client.get("/api/audit/summary", headers=headers)
        assert resp_sum.status_code == 200
        data_sum = resp_sum.json()
        assert data_sum["is_chain_intact"] is True
        assert data_sum["total_records"] == 0

        # 2. Получение списка логов (logs)
        resp_logs = client.get("/api/audit/logs", headers=headers)
        assert resp_logs.status_code == 200
        data_logs = resp_logs.json()
        assert "items" in data_logs
        assert "total" in data_logs

        # 3. Запуск криптографической верификации (verify)
        resp_ver = client.get("/api/audit/verify", headers=headers)
        assert resp_ver.status_code == 200
        data_ver = resp_ver.json()
        assert data_ver["is_valid"] is True
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_login_auditing_integration(monkeypatch):
    """
    Проверяет автоматическую фиксацию событий попыток входа в журнале аудита:
    AUTH_LOGIN_FAILED при неверном пароле и AUTH_LOGIN_SUCCESS при успехе.
    """
    mock_session = MockAuditSession()
    app.dependency_overrides[get_db] = lambda: mock_session

    # Настраиваем хэш пароля администратора
    monkeypatch.setattr(settings, "ADMIN_PASSWORD_HASH", get_password_hash("SecretPassword123"))
    monkeypatch.setattr(settings, "REQUIRE_2FA", False)
    monkeypatch.setattr(settings, "ADMIN_TOTP_SECRET", "")

    # Мок redis_client
    mock_redis = MagicMock()
    mock_redis.get.return_value = None
    monkeypatch.setattr(server, "redis_client", mock_redis)

    try:
        client = TestClient(app)

        # 1. Попытка входа с неверным паролем
        bad_login = client.post("/login", data={"username": "admin", "password": "WrongPassword"})
        assert bad_login.status_code == 400

        # Проверяем, что в журнале появилась запись о провале
        assert len(mock_session.logs) >= 1
        failed_entry = mock_session.logs[-1]
        assert failed_entry.action == "AUTH_LOGIN_FAILED"
        assert failed_entry.status == "FAILURE"
        assert failed_entry.actor == "admin"

        # 2. Успешный вход
        ok_login = client.post("/login", data={"username": "admin", "password": "SecretPassword123"})
        assert ok_login.status_code == 200

        # Проверяем запись об успехе
        success_entry = mock_session.logs[-1]
        assert success_entry.action == "AUTH_LOGIN_SUCCESS"
        assert success_entry.status == "SUCCESS"
        assert success_entry.prev_hash == failed_entry.record_hash
    finally:
        app.dependency_overrides.pop(get_db, None)
