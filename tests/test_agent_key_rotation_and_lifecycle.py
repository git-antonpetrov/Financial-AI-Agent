"""
Модульные и интеграционные тесты жизненного цикла, ротации и отзыва криптографических ключей агентов:
- Регистрация агента и выпуск первой версии ключа с генерацией Key ID (kid) и SHA-256 fingerprint;
- Плановая ротация ключа со сменой kid и архивацией предыдущего ключа в статус 'superseded';
- Отзыв скомпрометированного ключа администратором с мгновенной синхронизацией в Redis;
- Блокировка JWT-запросов, подписанных отозванным ключом (403 Forbidden);
- Аварийная блокировка (Emergency Kill Switch / Suspend) и реактивация агента;
- Проверка соответствия kid в заголовке JWT;
- Административный просмотр всех агентов и истории ключей.
"""

import os
import sys
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
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
    calculate_key_fingerprint,
    generate_key_id,
    revoke_agent_key_in_redis,
    is_agent_key_revoked_in_redis,
    verify_agent_jwt,
)
import server
from server import app
from db import models, crud


class InMemoryRedisMock:
    """Имитатор Redis для тестирования 2FA, черного списка и защиты от replay-атак."""
    def __init__(self):
        self.store = {}

    def exists(self, key: str) -> bool:
        return key in self.store

    def get(self, key: str):
        return self.store.get(key)

    def setex(self, key: str, ttl: int, value: str):
        self.store[key] = value

    def delete(self, key: str):
        self.store.pop(key, None)

    def incr(self, key: str):
        val = int(self.store.get(key, 0)) + 1
        self.store[key] = str(val)
        return val

    def expire(self, key: str, ttl: int):
        pass


class FakeAgentStore:
    """Имитатор хранилища базы данных для изолированного тестирования логики агентов и ключей."""
    def __init__(self):
        self.agents = {}  # name -> Agent
        self.keys = []    # list of AgentKey

    async def get_agent(self, db, name, include_keys=False):
        return self.agents.get(name)

    async def get_active_agent_key(self, db, name, kid=None):
        agent = self.agents.get(name)
        if not agent or agent.status != "active":
            return None
        active_keys = [k for k in self.keys if k.agent_id == agent.id and not k.is_revoked and k.status == "active"]
        if kid:
            active_keys = [k for k in active_keys if k.kid == kid]
        return active_keys[-1] if active_keys else None

    async def register_agent(self, db, name, public_key, ttl_days=90):
        agent = self.agents.get(name)
        now = datetime.now(timezone.utc)
        kid = generate_key_id(name, public_key)
        if agent:
            for k in self.keys:
                if k.agent_id == agent.id and k.status == "active":
                    k.status = "superseded"
            agent.public_key = public_key
            agent.status = "active"
        else:
            agent = models.Agent(id=len(self.agents) + 1, name=name, status="active", public_key=public_key, registered_at=now)
            agent.keys = []
            self.agents[name] = agent
        new_key = models.AgentKey(
            id=len(self.keys) + 1,
            agent_id=agent.id,
            kid=kid,
            public_key=public_key,
            status="active",
            is_revoked=False,
            created_at=now,
            expires_at=now + timedelta(days=ttl_days)
        )
        agent.keys.append(new_key)
        self.keys.append(new_key)
        return agent

    async def rotate_agent_key(self, db, name, new_public_key, ttl_days=90):
        agent = self.agents.get(name)
        if not agent:
            raise ValueError(f"Agent {name} not found")
        for k in self.keys:
            if k.agent_id == agent.id and k.status == "active":
                k.status = "superseded"
        agent.public_key = new_public_key
        agent.status = "active"
        now = datetime.now(timezone.utc)
        new_kid = generate_key_id(name, new_public_key)
        new_key = models.AgentKey(
            id=len(self.keys) + 1,
            agent_id=agent.id,
            kid=new_kid,
            public_key=new_public_key,
            status="active",
            is_revoked=False,
            created_at=now,
            expires_at=now + timedelta(days=ttl_days)
        )
        agent.keys.append(new_key)
        self.keys.append(new_key)
        return agent, new_key

    async def revoke_agent_key(self, db, name, kid=None, reason="Compromised"):
        agent = self.agents.get(name)
        if not agent:
            raise ValueError(f"Agent {name} not found")
        revoked = []
        for k in self.keys:
            if k.agent_id == agent.id and not k.is_revoked:
                if kid is None or k.kid == kid:
                    k.is_revoked = True
                    k.status = "revoked"
                    k.revoked_at = datetime.now(timezone.utc)
                    k.revocation_reason = reason
                    revoked.append(k)
        if not revoked:
            raise ValueError(f"No active unrevoked keys found for agent {name}")
        if agent.public_key in [r.public_key for r in revoked]:
            agent.public_key = None
        return revoked

    async def suspend_agent(self, db, name):
        agent = self.agents.get(name)
        if not agent:
            raise ValueError(f"Agent {name} not found")
        agent.status = "suspended"
        return agent

    async def reactivate_agent(self, db, name):
        agent = self.agents.get(name)
        if not agent:
            raise ValueError(f"Agent {name} not found")
        agent.status = "active"
        return agent

    async def list_all_agents(self, db):
        return list(self.agents.values())

    async def list_agent_keys(self, db, name):
        agent = self.agents.get(name)
        if not agent:
            return []
        return [k for k in self.keys if k.agent_id == agent.id]

    async def create_agent_request(self, db, agent_name, doc_ru, doc_en, just_ru, just_en):
        return models.AgentRequest(
            id=1,
            agent_name=agent_name,
            document_name_ru=doc_ru,
            document_name_en=doc_en,
            justification_ru=just_ru,
            justification_en=just_en,
            status="pending",
            created_at=datetime.now(timezone.utc)
        )


@pytest.fixture(autouse=True)
def setup_mocks(monkeypatch):
    """Автоматически подключает InMemoryRedisMock и FakeAgentStore во все тесты."""
    mock_redis = InMemoryRedisMock()
    monkeypatch.setattr(security, "redis_blacklist", mock_redis)
    monkeypatch.setattr(server, "redis_client", mock_redis)

    store = FakeAgentStore()
    monkeypatch.setattr(crud, "get_agent", store.get_agent)
    monkeypatch.setattr(crud, "get_active_agent_key", store.get_active_agent_key)
    monkeypatch.setattr(crud, "register_agent", store.register_agent)
    monkeypatch.setattr(crud, "rotate_agent_key", store.rotate_agent_key)
    monkeypatch.setattr(crud, "revoke_agent_key", store.revoke_agent_key)
    monkeypatch.setattr(crud, "suspend_agent", store.suspend_agent)
    monkeypatch.setattr(crud, "reactivate_agent", store.reactivate_agent)
    monkeypatch.setattr(crud, "list_all_agents", store.list_all_agents)
    monkeypatch.setattr(crud, "list_agent_keys", store.list_agent_keys)
    monkeypatch.setattr(crud, "create_agent_request", store.create_agent_request)

    # Заглушка зависимости get_db
    async def override_get_db():
        yield MagicMock()
    app.dependency_overrides[server.get_db] = override_get_db

    yield store

    app.dependency_overrides.clear()


def generate_test_rsa_keypair():
    """Генерирует пару RSA-ключей в формате PEM."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode("utf-8")
    pub_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("utf-8")
    return priv_pem, pub_pem


@pytest.fixture
def test_client():
    return TestClient(app)


@pytest.fixture
def admin_headers():
    token = create_access_token({"sub": "admin"})
    return {"Authorization": f"Bearer {token}"}


def test_agent_registration_and_initial_key(test_client):
    """Проверяет регистрацию нового агента и создание первой версии ключа с kid и fingerprint."""
    agent_name = "bank"
    bootstrap_token = "secret-bootstrap-bank-123"
    settings.AGENT_BANK_BOOTSTRAP_TOKEN = bootstrap_token

    _, pub_pem = generate_test_rsa_keypair()

    res = test_client.post(
        "/api/agents/register",
        json={"agent_name": agent_name, "public_key": pub_pem},
        headers={"X-Bootstrap-Token": bootstrap_token}
    )
    assert res.status_code == 200
    assert res.json()["status"] == "success"

    # Проверяем отпечаток ключа
    fingerprint = calculate_key_fingerprint(pub_pem)
    assert fingerprint.startswith("sha256:")
    assert len(fingerprint) > 10


def test_agent_key_rotation_via_endpoint(test_client, admin_headers):
    """Проверяет ротацию ключа агента: предыдущий ключ становится 'superseded', новый становится 'active'."""
    agent_name = "main"
    bootstrap_token = "secret-bootstrap-rotate-456"
    settings.AGENT_MAIN_BOOTSTRAP_TOKEN = bootstrap_token

    # 1. Первичная регистрация ключа #1
    _, pub_pem_v1 = generate_test_rsa_keypair()
    res1 = test_client.post(
        "/api/agents/register",
        json={"agent_name": agent_name, "public_key": pub_pem_v1},
        headers={"X-Bootstrap-Token": bootstrap_token}
    )
    assert res1.status_code == 200

    # 2. Ротация на ключ #2 через эндпоинт ротации агента
    _, pub_pem_v2 = generate_test_rsa_keypair()
    res_rot = test_client.post(
        "/api/agents/rotate",
        json={"agent_name": agent_name, "new_public_key": pub_pem_v2, "ttl_days": 60},
        headers={"X-Bootstrap-Token": bootstrap_token}
    )
    assert res_rot.status_code == 200
    rot_data = res_rot.json()
    assert rot_data["status"] == "success"
    new_kid = rot_data["key_id"]
    assert new_kid.startswith(f"{agent_name}-")

    # 3. Запрос списка ключей администратором: должно быть 2 ключа (один superseded, один active)
    res_keys = test_client.get(f"/api/agents/{agent_name}/keys", headers=admin_headers)
    assert res_keys.status_code == 200
    keys = res_keys.json()
    assert len(keys) >= 2

    active_keys = [k for k in keys if k["status"] == "active"]
    superseded_keys = [k for k in keys if k["status"] == "superseded"]
    assert len(active_keys) == 1
    assert active_keys[0]["kid"] == new_kid
    assert len(superseded_keys) >= 1


def test_admin_revoke_key_blocks_agent_requests(test_client, admin_headers):
    """Проверяет отзыв ключа администратором и немедленную блокировку запросов с отозванным ключом (403 Forbidden)."""
    agent_name = "invest"
    bootstrap_token = "secret-bootstrap-rev-789"
    settings.AGENT_INVEST_BOOTSTRAP_TOKEN = bootstrap_token

    priv_pem, pub_pem = generate_test_rsa_keypair()

    # 1. Регистрация агента
    res_reg = test_client.post(
        "/api/agents/register",
        json={"agent_name": agent_name, "public_key": pub_pem},
        headers={"X-Bootstrap-Token": bootstrap_token}
    )
    assert res_reg.status_code == 200

    # 2. Успешный запрос с валидным JWT
    payload = {
        "agent_name": agent_name,
        "document_name_ru": "Федеральный закон №395-1",
        "document_name_en": "Federal Law No. 395-1",
        "justification_ru": "О регулировании банковской тайны",
        "justification_en": "Banking secrecy regulation",
        "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())
    }
    valid_jwt = jwt.encode(payload, priv_pem, algorithm="RS256")
    res_req1 = test_client.post("/api/agent_requests", json={"token": valid_jwt})
    assert res_req1.status_code == 200

    # 3. Администратор отзывает ключ
    res_revoke = test_client.post(
        f"/api/agents/{agent_name}/revoke",
        json={"agent_name": agent_name, "reason": "Private key leakage detected"},
        headers=admin_headers
    )
    assert res_revoke.status_code == 200
    assert res_revoke.json()["status"] == "success"

    # 4. Повторный запрос с токеном от этого ключа немедленно блокируется со статусом 403
    res_req2 = test_client.post("/api/agent_requests", json={"token": valid_jwt})
    assert res_req2.status_code == 403
    assert "revoked" in res_req2.json()["detail"].lower()


def test_emergency_agent_suspend_and_reactivate(test_client, admin_headers):
    """Проверяет Emergency Kill Switch: блокировку всех запросов приостановленного агента и его восстановление."""
    agent_name = "digital"
    bootstrap_token = "secret-bootstrap-susp-101"
    settings.AGENT_DIGITAL_BOOTSTRAP_TOKEN = bootstrap_token

    priv_pem, pub_pem = generate_test_rsa_keypair()
    res_reg = test_client.post(
        "/api/agents/register",
        json={"agent_name": agent_name, "public_key": pub_pem},
        headers={"X-Bootstrap-Token": bootstrap_token}
    )
    assert res_reg.status_code == 200

    token_payload = {
        "agent_name": agent_name,
        "document_name_ru": "Регламент защиты информации",
        "justification_ru": "Требования ИБ",
        "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())
    }
    agent_jwt = jwt.encode(token_payload, priv_pem, algorithm="RS256")

    # 1. Приостановка агента
    res_suspend = test_client.post(f"/api/agents/{agent_name}/suspend", headers=admin_headers)
    assert res_suspend.status_code == 200
    assert "suspended" in res_suspend.json()["message"]

    # 2. Попытка создания заявки блокируется
    res_req_blocked = test_client.post("/api/agent_requests", json={"token": agent_jwt})
    assert res_req_blocked.status_code == 403
    assert "suspended" in res_req_blocked.json()["detail"].lower() or "revoked" in res_req_blocked.json()["detail"].lower()

    # 3. Реактивация агента администратором
    res_reactivate = test_client.post(f"/api/agents/{agent_name}/reactivate", headers=admin_headers)
    assert res_reactivate.status_code == 200

    # 4. Запрос снова успешно проходит
    res_req_ok = test_client.post("/api/agent_requests", json={"token": agent_jwt})
    assert res_req_ok.status_code == 200


def test_kid_matching_in_jwt_header(test_client):
    """Проверяет валидацию kid из заголовка JWT."""
    agent_name = "main"
    bootstrap_token = "secret-bootstrap-kid-202"
    settings.AGENT_MAIN_BOOTSTRAP_TOKEN = bootstrap_token

    priv_pem, pub_pem = generate_test_rsa_keypair()
    res_reg = test_client.post(
        "/api/agents/register",
        json={"agent_name": agent_name, "public_key": pub_pem},
        headers={"X-Bootstrap-Token": bootstrap_token}
    )
    assert res_reg.status_code == 200

    expected_kid = generate_key_id(agent_name, pub_pem)

    payload = {
        "agent_name": agent_name,
        "document_name_ru": "Инструкция ЦБ 199-И",
        "justification_ru": "Нормативы ликвидности",
        "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())
    }

    # 1. JWT с корректным kid принимается
    jwt_correct_kid = jwt.encode(payload, priv_pem, algorithm="RS256", headers={"kid": expected_kid})
    res_ok = test_client.post("/api/agent_requests", json={"token": jwt_correct_kid})
    assert res_ok.status_code == 200

    # 2. JWT с несуществующим / несовпадающим kid отклоняется со статусом 403
    jwt_wrong_kid = jwt.encode(payload, priv_pem, algorithm="RS256", headers={"kid": "non-existent-kid-999"})
    res_fail = test_client.post("/api/agent_requests", json={"token": jwt_wrong_kid})
    assert res_fail.status_code == 403


def test_admin_list_agents_endpoint(test_client, admin_headers):
    """Проверяет получение администратором списка всех зарегистрированных агентов со статусами."""
    res = test_client.get("/api/agents", headers=admin_headers)
    assert res.status_code == 200
    agents = res.json()
    assert isinstance(agents, list)
    if len(agents) > 0:
        a = agents[0]
        assert "id" in a
        assert "name" in a
        assert "status" in a
        assert "keys" in a


def test_fingerprint_and_kid_deterministic_generation():
    """Проверяет детерминированное вычисление fingerprint и kid."""
    _, pub_pem = generate_test_rsa_keypair()
    fp1 = calculate_key_fingerprint(pub_pem)
    fp2 = calculate_key_fingerprint(pub_pem)
    assert fp1 == fp2
    assert fp1.startswith("sha256:")

    kid1 = generate_key_id("bank", pub_pem)
    kid2 = generate_key_id("bank", pub_pem)
    assert kid1 == kid2
    assert kid1.startswith("bank-")


def test_redis_revocation_helpers():
    """Проверяет работу функций черного списка Redis."""
    agent_name = "test_redis_agent"
    kid = "test-kid-12345"

    assert is_agent_key_revoked_in_redis(agent_name, kid) is False

    # Отзываем конкретный ключ и агента
    revoke_agent_key_in_redis(agent_name, kid)
    assert is_agent_key_revoked_in_redis(agent_name, kid) is True
    assert is_agent_key_revoked_in_redis(agent_name) is True

