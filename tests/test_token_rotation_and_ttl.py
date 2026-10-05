"""
Модульные и интеграционные тесты для проверки ротации токенов (RTR) и контроля TTL:
- Короткий срок жизни access-токена (15 минут);
- Долгоживущий refresh-токен (7 дней);
- Запрет использования refresh-токена в качестве access-токена;
- Одноразовость refresh-токенов и детекция повторного использования (Reuse Detection);
- Эндпоинты /api/auth/refresh и /api/auth/logout с отзывом обоих токенов.
"""

import os
import sys
import pytest
import jwt
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

# Предварительный импорт fastapi до манипуляций с sys.path
import fastapi
from fastapi import HTTPException
from fastapi.testclient import TestClient

# Моки внешних сервисов
for mod in ["redis", "minio", "litellm", "asyncpg"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

# Настройка путей импорта admin_server
sys.path.insert(0, os.path.abspath("src/admin_server"))
sys.path.insert(0, os.path.abspath("src/admin_server/fastapi"))

import security
from security import (
    settings,
    create_access_token,
    create_refresh_token,
    decode_access_token,
    verify_and_rotate_refresh_token,
    get_current_admin,
)


class InMemoryRedisMock:
    """Имитатор Redis для тестирования черного списка и ротации токенов в памяти."""
    def __init__(self):
        self.store = {}

    def exists(self, key: str) -> bool:
        return key in self.store

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


@pytest.fixture(autouse=True)
def setup_mock_redis(monkeypatch):
    """Изолирует хранилище Redis для каждого теста."""
    mock_redis = InMemoryRedisMock()
    monkeypatch.setattr(security, "redis_blacklist", mock_redis)
    return mock_redis


def test_access_token_short_lifespan_and_claims():
    """Проверяет короткий срок действия access-токена (15 минут) и наличие type=access."""
    token = create_access_token({"sub": "admin"})
    payload = decode_access_token(token)

    assert payload.get("sub") == "admin"
    assert payload.get("type") == "access"
    assert "jti" in payload
    assert "iat" in payload
    assert "exp" in payload

    ttl_seconds = payload["exp"] - payload["iat"]
    expected_seconds = settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    assert abs(ttl_seconds - expected_seconds) <= 5
    assert ttl_seconds <= 15 * 60 + 5


def test_refresh_token_lifespan_and_claims():
    """Проверяет срок действия refresh-токена (7 дней) и наличие type=refresh."""
    refresh_token = create_refresh_token({"sub": "admin"})
    payload = decode_access_token(refresh_token)

    assert payload.get("sub") == "admin"
    assert payload.get("type") == "refresh"
    assert "jti" in payload

    ttl_seconds = payload["exp"] - payload["iat"]
    expected_seconds = settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400
    assert abs(ttl_seconds - expected_seconds) <= 5


@pytest.mark.anyio
async def test_refresh_token_cannot_be_used_as_access_token():
    """Проверяет запрет использования refresh-токена для доступа к API (get_current_admin)."""
    refresh_token = create_refresh_token({"sub": "admin"})

    with pytest.raises(HTTPException) as exc_info:
        await get_current_admin(refresh_token)
    assert exc_info.value.status_code == 401


def test_refresh_token_rotation_and_invalidation(setup_mock_redis):
    """Проверяет успешную ротацию токена и инвалидацию старого refresh-токена."""
    old_refresh = create_refresh_token({"sub": "admin"})
    old_payload = decode_access_token(old_refresh)
    old_jti = old_payload["jti"]

    new_access, new_refresh = verify_and_rotate_refresh_token(old_refresh)

    assert new_access != old_refresh
    assert new_refresh != old_refresh
    assert decode_access_token(new_access)["type"] == "access"
    assert decode_access_token(new_refresh)["type"] == "refresh"

    # Старый токен должен оказаться в черном списке
    assert setup_mock_redis.exists(f"blacklist:{old_jti}")
    assert setup_mock_redis.exists(f"blacklist:{old_refresh}")


def test_refresh_token_reuse_detection_blocked(setup_mock_redis):
    """Проверяет блокировку повторного использования одного и того же refresh-токена (Reuse Detection)."""
    refresh_token = create_refresh_token({"sub": "admin"})

    # Первая ротация — успешна
    verify_and_rotate_refresh_token(refresh_token)

    # Вторая попытка с тем же refresh-токеном — должна быть заблокирована с 401
    with pytest.raises(HTTPException) as exc_info:
        verify_and_rotate_refresh_token(refresh_token)
    assert exc_info.value.status_code == 401
    assert "already used or revoked" in exc_info.value.detail


def test_api_auth_refresh_endpoint(setup_mock_redis, monkeypatch):
    """Интеграционный тест FastAPI эндпоинта /api/auth/refresh."""
    from server import app
    client = TestClient(app)

    # 1. Создаем начальный refresh токен
    initial_refresh = create_refresh_token({"sub": "admin"})

    # 2. Вызываем /api/auth/refresh
    response = client.post("/api/auth/refresh", json={"refresh_token": initial_refresh})
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "bearer"
    assert data["expires_in"] == settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60

    # 3. Повторный вызов со старым токеном должен вернуть 401 Unauthorized
    reuse_response = client.post("/api/auth/refresh", json={"refresh_token": initial_refresh})
    assert reuse_response.status_code == 401


def test_api_auth_logout_revokes_both_tokens(setup_mock_redis):
    """Интеграционный тест /api/auth/logout с отзывом access и refresh токенов."""
    from server import app
    client = TestClient(app)

    access_token = create_access_token({"sub": "admin"})
    refresh_token = create_refresh_token({"sub": "admin"})
    access_jti = decode_access_token(access_token)["jti"]
    refresh_jti = decode_access_token(refresh_token)["jti"]

    logout_res = client.post(
        "/api/auth/logout",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"refresh_token": refresh_token}
    )
    assert logout_res.status_code == 200

    # Проверяем наличие обоих в черном списке
    assert setup_mock_redis.exists(f"blacklist:{access_jti}")
    assert setup_mock_redis.exists(f"blacklist:{refresh_jti}")
