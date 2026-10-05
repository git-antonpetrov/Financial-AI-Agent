"""
Тесты для ролевой модели доступа (RBAC / Role-Based Access Control).
Проверяют принцип наименьших привилегий (Least Privilege), генерацию токенов
с ролями и разрешениями, обратную совместимость CurrentUser,
FastAPI-зависимости require_role и require_permission,
а также разграничение прав на эндпоинтах для superadmin, operator и auditor.
"""

import os
import sys
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from fastapi import HTTPException, status
from fastapi.testclient import TestClient

# Предварительная настройка окружения
sys.path.insert(0, os.path.abspath("src/admin_server"))
sys.path.insert(0, os.path.abspath("src/admin_server/fastapi"))
sys.path.insert(0, os.path.abspath("src/admin_server/vectors"))

os.environ["POSTGRES_PASSWORD"] = "test_postgres_secret"

# Изоляция внешних сервисов
for mod in ["redis", "litellm", "asyncpg", "psycopg2", "psycopg2.pool", "chromadb", "chromadb.config", "langchain_text_splitters"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

import security
from security import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    verify_and_rotate_refresh_token,
    get_current_admin,
    get_current_user,
    require_role,
    require_permission,
)
from rbac import (
    Role,
    Permission,
    ROLE_PERMISSIONS,
    ROLE_DESCRIPTIONS,
    CurrentUser,
    get_permissions_for_role,
)


# --- 1. ТЕСТЫ СТРУКТУРЫ RBAC И КЛАССА CurrentUser ---

def test_role_and_permission_definitions():
    """Проверяет корректность определения ролей и матрицы разрешений."""
    assert Role.SUPERADMIN.value == "superadmin"
    assert Role.OPERATOR.value == "operator"
    assert Role.AUDITOR.value == "auditor"

    superadmin_perms = get_permissions_for_role(Role.SUPERADMIN)
    operator_perms = get_permissions_for_role(Role.OPERATOR)
    auditor_perms = get_permissions_for_role(Role.AUDITOR)

    # Superadmin обладает всеми разрешениями
    assert len(superadmin_perms) == len(Permission)
    assert Permission.SECURITY_MANAGE in superadmin_perms
    assert Permission.KEYS_REVOKE in superadmin_perms
    assert Permission.AUDIT_VERIFY in superadmin_perms

    # Operator имеет доступ к документам и заявкам, но не имеет прав на отзыв ключей, блокировку и аудит
    assert Permission.DOCUMENTS_READ in operator_perms
    assert Permission.DOCUMENTS_WRITE in operator_perms
    assert Permission.DOCUMENTS_DELETE in operator_perms
    assert Permission.REQUESTS_MANAGE in operator_perms
    assert Permission.KEYS_ROTATE in operator_perms
    assert Permission.KEYS_REVOKE not in operator_perms
    assert Permission.AGENTS_MANAGE not in operator_perms
    assert Permission.AUDIT_READ not in operator_perms
    assert Permission.SECURITY_MANAGE not in operator_perms

    # Auditor имеет Read-Only доступ и право верификации аудита
    assert Permission.DOCUMENTS_READ in auditor_perms
    assert Permission.REQUESTS_READ in auditor_perms
    assert Permission.AGENTS_READ in auditor_perms
    assert Permission.AUDIT_READ in auditor_perms
    assert Permission.AUDIT_VERIFY in auditor_perms
    assert Permission.DOCUMENTS_WRITE not in auditor_perms
    assert Permission.DOCUMENTS_DELETE not in auditor_perms
    assert Permission.KEYS_ROTATE not in auditor_perms
    assert Permission.KEYS_REVOKE not in auditor_perms


def test_current_user_compatibility():
    """Проверяет обратную совместимость CurrentUser со строковым типом."""
    user = CurrentUser(username="admin", role=Role.SUPERADMIN)
    
    # 100% совместимость с str
    assert isinstance(user, str)
    assert user == "admin"
    assert user.username == "admin"
    assert f"User: {user}" == "User: admin"

    # Методы проверки прав
    assert user.has_role(Role.SUPERADMIN)
    assert user.has_role("superadmin")
    assert not user.has_role(Role.OPERATOR)
    assert user.has_permission(Permission.DOCUMENTS_READ)
    assert user.has_permission("documents:read")
    assert not user.has_permission("nonexistent:perm")


# --- 2. ТЕСТЫ JWT-ТОКЕНОВ С ПОДДЕРЖКОЙ RBAC ---

def test_create_access_token_with_roles():
    """Проверяет генерацию и декодирование JWT токенов для разных ролей."""
    # 1. Superadmin (по умолчанию)
    admin_token = create_access_token({"sub": "admin"})
    payload_admin = decode_access_token(admin_token)
    assert payload_admin["role"] == "superadmin"
    assert "security:manage" in payload_admin["permissions"]
    assert "keys:revoke" in payload_admin["permissions"]

    # 2. Operator
    op_token = create_access_token({"sub": "operator"}, role=Role.OPERATOR)
    payload_op = decode_access_token(op_token)
    assert payload_op["role"] == "operator"
    assert "documents:write" in payload_op["permissions"]
    assert "keys:revoke" not in payload_op["permissions"]
    assert "audit:read" not in payload_op["permissions"]

    # 3. Auditor
    aud_token = create_access_token({"sub": "auditor"}, role=Role.AUDITOR)
    payload_aud = decode_access_token(aud_token)
    assert payload_aud["role"] == "auditor"
    assert "audit:read" in payload_aud["permissions"]
    assert "audit:verify" in payload_aud["permissions"]
    assert "documents:write" not in payload_aud["permissions"]


def test_refresh_token_rotation_preserves_role():
    """Проверяет сохранение роли пользователя при ротации refresh-токена (RTR)."""
    if security.redis_blacklist is not None:
        security.redis_blacklist.exists.return_value = False
        security.redis_blacklist.setex.return_value = True

    op_refresh = create_refresh_token({"sub": "operator"}, role=Role.OPERATOR)
    new_acc, new_ref = verify_and_rotate_refresh_token(op_refresh)

    acc_payload = decode_access_token(new_acc)
    ref_payload = decode_access_token(new_ref)
    assert acc_payload["role"] == "operator"
    assert ref_payload["role"] == "operator"
    assert "documents:write" in acc_payload["permissions"]
    assert "keys:revoke" not in acc_payload["permissions"]


# --- 3. ТЕСТЫ FASTAPI-ЗАВИСИМОСТЕЙ require_role И require_permission ---

@pytest.mark.anyio
async def test_require_role_dependency():
    """Проверяет логику проверки ролей зависимостью require_role."""
    op_user = CurrentUser("operator", role=Role.OPERATOR)
    admin_user = CurrentUser("admin", role=Role.SUPERADMIN)

    # Проверка роли OPERATOR
    op_checker = require_role(Role.OPERATOR)
    assert await op_checker(op_user) == op_user

    # OPERATOR не может пройти проверку на SUPERADMIN -> 403 Forbidden
    super_checker = require_role(Role.SUPERADMIN)
    with pytest.raises(HTTPException) as exc_info:
        await super_checker(op_user)
    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
    assert "insufficient role privileges" in exc_info.value.detail

    # SUPERADMIN успешно проходит
    assert await super_checker(admin_user) == admin_user


@pytest.mark.anyio
async def test_require_permission_dependency():
    """Проверяет логику проверки гранулярных разрешений require_permission."""
    op_user = CurrentUser("operator", role=Role.OPERATOR)
    aud_user = CurrentUser("auditor", role=Role.AUDITOR)

    # Оператор имеет documents:write
    write_checker = require_permission(Permission.DOCUMENTS_WRITE)
    assert await write_checker(op_user) == op_user

    # Аудитор НЕ имеет documents:write -> 403 Forbidden
    with pytest.raises(HTTPException) as exc_info1:
        await write_checker(aud_user)
    assert exc_info1.value.status_code == status.HTTP_403_FORBIDDEN
    assert "documents:write" in exc_info1.value.detail

    # Аудитор имеет audit:read
    audit_checker = require_permission(Permission.AUDIT_READ)
    assert await audit_checker(aud_user) == aud_user

    # Оператор НЕ имеет audit:read -> 403 Forbidden
    with pytest.raises(HTTPException) as exc_info2:
        await audit_checker(op_user)
    assert exc_info2.value.status_code == status.HTTP_403_FORBIDDEN
    assert "audit:read" in exc_info2.value.detail


# --- 4. ИНТЕГРАЦИОННЫЕ ТЕСТЫ API С РАЗЛИЧНЫМИ РОЛЯМИ ---

def test_api_auth_me_endpoint():
    """Проверяет эндпоинт GET /api/auth/me для текущего авторизованного пользователя."""
    from server import app
    client = TestClient(app)

    # Тестируем для оператора
    op_token = create_access_token({"sub": "operator"}, role=Role.OPERATOR)
    headers = {"Authorization": f"Bearer {op_token}"}
    resp = client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["username"] == "operator"
    assert data["role"] == "operator"
    assert "documents:write" in data["permissions"]
    assert "keys:revoke" not in data["permissions"]


def test_api_auth_roles_endpoint():
    """Проверяет эндпоинт GET /api/auth/roles с полной матрицей ролей."""
    from server import app
    client = TestClient(app)

    admin_token = create_access_token({"sub": "admin"}, role=Role.SUPERADMIN)
    headers = {"Authorization": f"Bearer {admin_token}"}
    resp = client.get("/api/auth/roles", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    role_names = [r["role"] for r in data["roles"]]
    assert "superadmin" in role_names
    assert "operator" in role_names
    assert "auditor" in role_names
    assert len(data["all_permissions"]) == len(Permission)


def test_operator_access_restrictions():
    """Проверяет, что оператор имеет доступ к своим операциям, но заблокирован на аудите и отзыве ключей."""
    from server import app
    client = TestClient(app)

    op_token = create_access_token({"sub": "operator"}, role=Role.OPERATOR)
    headers = {"Authorization": f"Bearer {op_token}"}

    # 1. Запрет на доступ к журналу аудита (audit:read) -> 403
    resp_audit = client.get("/api/audit/logs", headers=headers)
    assert resp_audit.status_code == 403
    assert "audit:read" in resp_audit.json()["detail"]

    # 2. Запрет на верификацию аудита (audit:verify) -> 403
    resp_verify = client.get("/api/audit/verify", headers=headers)
    assert resp_verify.status_code == 403
    assert "audit:verify" in resp_verify.json()["detail"]

    # 3. Запрет на отзыв ключа агента (keys:revoke) -> 403
    resp_revoke = client.post("/api/agents/main/revoke", json={"agent_name": "main", "reason": "test"}, headers=headers)
    assert resp_revoke.status_code == 403
    assert "keys:revoke" in resp_revoke.json()["detail"]

    # 4. Запрет на настройку 2FA (security:manage) -> 403
    resp_2fa = client.get("/api/auth/2fa/setup", headers=headers)
    assert resp_2fa.status_code == 403
    assert "security:manage" in resp_2fa.json()["detail"]


def test_auditor_access_restrictions():
    """Проверяет, что аудитор имеет Read-Only доступ, но заблокирован на мутациях документов и ключей."""
    from server import app
    client = TestClient(app)

    aud_token = create_access_token({"sub": "auditor"}, role=Role.AUDITOR)
    headers = {"Authorization": f"Bearer {aud_token}"}

    # 1. Запрет на выгрузку документа (documents:write) -> 403
    files = {"file": ("test.md", b"# Content", "text/markdown")}
    data = {"file_hash": "1234567890abcdef"}
    resp_upload = client.post("/api/upload/main/upsert", files=files, data=data, headers=headers)
    assert resp_upload.status_code == 403
    assert "documents:write" in resp_upload.json()["detail"]

    # 2. Запрет на одобрение заявок (requests:manage) -> 403
    resp_approve = client.post("/api/agent-requests/approve", json={"request_ids": [1]}, headers=headers)
    assert resp_approve.status_code == 403
    assert "requests:manage" in resp_approve.json()["detail"]

    # 3. Запрет на ротацию ключа (keys:rotate) -> 403
    resp_rotate = client.post("/api/agents/main/rotate", json={"agent_name": "main", "new_public_key": "dummy"}, headers=headers)
    assert resp_rotate.status_code == 403
    assert "keys:rotate" in resp_rotate.json()["detail"]


def test_login_supports_x_role_header():
    """Проверяет выпуск токенов с указанной ролью при логине через заголовок X-Role."""
    from server import app, settings
    client = TestClient(app)

    settings.ADMIN_PASSWORD_HASH = security.get_password_hash("SecretPassword123!")
    settings.REQUIRE_2FA = False
    settings.ADMIN_TOTP_SECRET = ""

    # Логин с запросом роли operator
    resp = client.post(
        "/login",
        data={"username": "admin", "password": "SecretPassword123!"},
        headers={"X-Role": "operator"}
    )
    assert resp.status_code == 200
    token_data = resp.json()
    payload = decode_access_token(token_data["access_token"])
    assert payload["role"] == "operator"
    assert "documents:write" in payload["permissions"]
    assert "keys:revoke" not in payload["permissions"]
