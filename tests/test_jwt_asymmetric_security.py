"""
Модульные тесты для проверки асимметричной криптографии JWT (RS256) в admin_server.
Проверяют генерацию, валидацию, защиту от подделки, алгоритмических атак и экспорт публичного ключа.
"""

import os
import sys
import tempfile
import pytest
import jwt
from unittest.mock import MagicMock
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

# Предварительно импортируем настоящий пакет fastapi до добавления локальных путей в sys.path
import fastapi
from fastapi import HTTPException
from fastapi.testclient import TestClient

# Моки внешних сервисов для изоляции тестов
for mod in ["redis", "minio", "litellm", "asyncpg"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

# Настройка путей для импорта модулей admin_server
sys.path.insert(0, os.path.abspath("src/admin_server"))
sys.path.insert(0, os.path.abspath("src/admin_server/fastapi"))

import security
from security import (
    Settings,
    create_access_token,
    decode_access_token,
    get_current_admin,
    verify_password,
    get_password_hash,
)


def test_rs256_token_generation_and_validation():
    """Проверяет создание и верификацию JWT-токена по алгоритму RS256."""
    token = create_access_token({"sub": "admin", "custom_role": "superadmin"})
    
    # 1. Заголовок токена должен строго содержать alg=RS256
    unverified_headers = jwt.get_unverified_header(token)
    assert unverified_headers.get("alg") == "RS256"
    assert unverified_headers.get("typ") == "JWT"

    # 2. Декодирование через decode_access_token (публичный ключ сервера)
    payload = decode_access_token(token)
    assert payload["sub"] == "admin"
    assert payload["custom_role"] == "superadmin"
    assert "jti" in payload
    assert "exp" in payload
    assert "iat" in payload


def test_symmetric_hs256_token_rejected():
    """Проверяет отклонение токенов с симметричной подписью (защита от Algorithm Confusion)."""
    # Злоумышленник пытается подписать токен симметричным HS256
    fake_secret = "secret_key_used_by_attacker_123456789"
    payload = {"sub": "admin", "exp": 9999999999}
    forged_token = jwt.encode(payload, fake_secret, algorithm="HS256")

    # Валидатор сервера должен отклонить токен из-за несоответствия алгоритма
    with pytest.raises(jwt.InvalidAlgorithmError):
        decode_access_token(forged_token)


def test_untrusted_rsa_key_rejected():
    """Проверяет отклонение токена, подписанного чужой (недоверенной) ключевой парой RSA."""
    untrusted_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    untrusted_priv_pem = untrusted_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode("utf-8")

    payload = {"sub": "admin", "exp": 9999999999}
    forged_token = jwt.encode(payload, untrusted_priv_pem, algorithm="RS256")

    with pytest.raises(jwt.InvalidSignatureError):
        decode_access_token(forged_token)


def test_tampered_payload_rejected():
    """Проверяет, что изменение полезной нагрузки нарушает RSA-подпись."""
    token = create_access_token({"sub": "admin"})
    parts = token.split(".")
    assert len(parts) == 3

    # Подделка среднего сегмента (payload)
    tampered_token = f"{parts[0]}.eyAic3ViIjogImF0dGFja2VyIiB9.{parts[2]}"

    with pytest.raises(jwt.InvalidSignatureError):
        decode_access_token(tampered_token)


@pytest.mark.anyio
async def test_get_current_admin_valid_and_invalid():
    """Проверяет dependency get_current_admin на валидном и невалидном токене."""
    from fastapi import HTTPException
    import security

    if security.redis_blacklist is not None:
        security.redis_blacklist.exists.return_value = False

    valid_token = create_access_token({"sub": "admin"})
    admin_user = await get_current_admin(valid_token)
    assert admin_user == "admin"

    # Невалидный sub
    bad_sub_token = create_access_token({"sub": "impostor"})
    with pytest.raises(HTTPException) as exc_info:
        await get_current_admin(bad_sub_token)
    assert exc_info.value.status_code == 401

    # Мусорная строка вместо токена
    with pytest.raises(HTTPException) as exc_info2:
        await get_current_admin("totally.invalid.token")
    assert exc_info2.value.status_code == 401


def test_key_loading_from_file_and_derivation():
    """Проверяет загрузку приватного ключа из файла и автоматическую деривацию публичного ключа."""
    # Генерируем тестовую пару
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode("utf-8")
    expected_pub_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("utf-8")

    with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".pem") as f:
        f.write(priv_pem)
        temp_priv_path = f.name

    try:
        old_path = os.environ.get("JWT_PRIVATE_KEY_PATH")
        os.environ["JWT_PRIVATE_KEY_PATH"] = temp_priv_path
        # Очищаем публичный путь и строковые ключи для проверки деривации
        os.environ.pop("JWT_PUBLIC_KEY_PATH", None)
        os.environ.pop("JWT_PRIVATE_KEY", None)
        os.environ.pop("JWT_PUBLIC_KEY", None)

        custom_settings = Settings()
        assert custom_settings.PRIVATE_KEY.strip() == priv_pem.strip()
        assert custom_settings.PUBLIC_KEY.strip() == expected_pub_pem.strip()
    finally:
        if old_path is not None:
            os.environ["JWT_PRIVATE_KEY_PATH"] = old_path
        else:
            os.environ.pop("JWT_PRIVATE_KEY_PATH", None)
        if os.path.exists(temp_priv_path):
            os.remove(temp_priv_path)


def test_validate_production_env():
    """Проверяет валидацию наличия RSA-ключа для продакшена."""
    test_settings = Settings()
    
    # Сохраняем исходное окружение
    saved_env = {k: os.environ.get(k) for k in [
        "JWT_PRIVATE_KEY", "JWT_PRIVATE_KEY_PATH",
        "AGENT_DIGITAL_BOOTSTRAP_TOKEN", "AGENT_BANK_BOOTSTRAP_TOKEN",
        "AGENT_INVEST_BOOTSTRAP_TOKEN", "AGENT_MAIN_BOOTSTRAP_TOKEN"
    ]}

    try:
        # Убираем приватные ключи
        os.environ.pop("JWT_PRIVATE_KEY", None)
        os.environ.pop("JWT_PRIVATE_KEY_PATH", None)
        for a in ["DIGITAL", "BANK", "INVEST", "MAIN"]:
            os.environ[f"AGENT_{a}_BOOTSTRAP_TOKEN"] = "token"

        with pytest.raises(RuntimeError) as exc_info:
            test_settings.validate_production_env()
        assert "JWT_PRIVATE_KEY" in str(exc_info.value)
    finally:
        for k, v in saved_env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)


def test_public_key_endpoint():
    """Проверяет эндпоинт /api/auth/public-key в FastAPI."""
    from fastapi.testclient import TestClient
    from server import app

    client = TestClient(app)
    response = client.get("/api/auth/public-key")
    assert response.status_code == 200
    data = response.json()
    assert data["algorithm"] == "RS256"
    assert "BEGIN PUBLIC KEY" in data["public_key"]
