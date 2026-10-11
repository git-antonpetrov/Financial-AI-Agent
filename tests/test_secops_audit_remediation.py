"""
Тесты для устранения замечаний независимого аудита безопасности:
SEC-07: Запрет дефолтной роли SUPERADMIN при отсутствии или невалидной роли в токене (401).
SEC-10: Строгая валидация режима STRICT_SECURITY (REQUIRE_MTLS, REQUIRE_2FA, слабые пароли).
SEC-11: Поддержка Key ID (KID) в формате ENC2 с обратной совместимостью для legacy ENC1.
SEC-12: Прерывание операции с 500 Storage failure при сбое MinIO и удаление осиротевших записей.
SEC-15: Маскирование CONTENTAI_PASSWORD как "********" на эндпоинте /api/config/contentai.
SEC-16: Обязательная проверка срока действия токена (exp) для админа и агентов.
SEC-17: Неблокирующий слушатель отзывов Redis в отдельном потоке (asyncio.to_thread).
SEC-18: Очистка пула соединений engine.dispose() и закрытие redis_client при shutdown.
SEC-19: Логирование предупреждений при сбоях advisory lock без тихого подавления.
SEC-24: Логирование сбоев Redis в rate limiter и блокировке подбора паролей.
SEC-26: Маскирование одноразовых TOTP кодов в логах и усиленная деривация ключей derive_key (PBKDF2/HKDF).
"""

import os
import sys
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock, patch
import pytest
import jwt
from fastapi import HTTPException, status
from fastapi.testclient import TestClient

# Пути к исходным модулям
sys.path.insert(0, os.path.abspath("src/admin_server"))
sys.path.insert(0, os.path.abspath("src/admin_server/fastapi"))
sys.path.insert(0, os.path.abspath("src/admin_server/vectors"))

os.environ["POSTGRES_PASSWORD"] = "test_postgres_secret"

import security
from security import (
    settings,
    create_access_token,
    decode_access_token,
    get_current_admin,
    get_current_user,
    verify_agent_jwt,
    encrypt_data_at_rest,
    decrypt_data_at_rest,
    encrypt_text_at_rest,
    decrypt_text_at_rest,
    derive_key,
    resolve_encryption_key,
    MAGIC_ENCRYPTION_V1,
    MAGIC_ENCRYPTION_V2_PREFIX,
    Role,
)
from vectors.chroma_envelope import (
    encrypt_chroma_chunk,
    decrypt_chroma_chunk,
    is_encrypted_chunk,
    MAGIC_ENVELOPE_V1,
    MAGIC_ENVELOPE_V2_PREFIX,
)
import server
from server import app


# --- SEC-07: Запрет дефолтной роли SUPERADMIN ---

@pytest.mark.anyio
async def test_sec07_missing_role_raises_401():
    """Токен без клейма role отклоняется с HTTP 401 detail='Invalid or missing role in token'."""
    now = datetime.now(timezone.utc)
    raw_payload = {
        "sub": "admin",
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=15),
        "jti": str(uuid.uuid4()),
    }
    tok = jwt.encode(raw_payload, settings.PRIVATE_KEY, algorithm=settings.ALGORITHM)
    with pytest.raises(HTTPException) as exc:
        await get_current_admin(tok)
    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "Invalid or missing role in token" in exc.value.detail


@pytest.mark.anyio
async def test_sec07_invalid_role_raises_401():
    """Токен с неизвестной или некорректной ролью отклоняется с HTTP 401."""
    now = datetime.now(timezone.utc)
    raw_payload = {
        "sub": "admin",
        "role": "attacker_super_privilege",
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=15),
        "jti": str(uuid.uuid4()),
    }
    tok = jwt.encode(raw_payload, settings.PRIVATE_KEY, algorithm=settings.ALGORITHM)
    with pytest.raises(HTTPException) as exc:
        await get_current_admin(tok)
    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "Invalid or missing role in token" in exc.value.detail


# --- SEC-10: Строгая валидация STRICT_SECURITY ---

def test_sec10_strict_security_requires_mtls():
    """При STRICT_SECURITY=true отсутствие REQUIRE_MTLS вызывает RuntimeError."""
    with patch.dict(os.environ, {"STRICT_SECURITY": "true", "REQUIRE_MTLS": "false"}):
        with patch.object(settings, "REQUIRE_MTLS", False):
            with pytest.raises(RuntimeError, match="STRICT_SECURITY: REQUIRE_MTLS must be true"):
                settings.validate_production_env()


def test_sec10_strict_security_requires_2fa():
    """При STRICT_SECURITY=true отсутствие REQUIRE_2FA вызывает RuntimeError."""
    with patch.dict(os.environ, {"STRICT_SECURITY": "true", "REQUIRE_MTLS": "true", "REQUIRE_2FA": "false"}):
        with patch.object(settings, "REQUIRE_MTLS", True):
            with patch.object(settings, "REQUIRE_2FA", False):
                with pytest.raises(RuntimeError, match="STRICT_SECURITY: REQUIRE_2FA must be true"):
                    settings.validate_production_env()


def test_sec10_strict_security_forbids_default_passwords():
    """При STRICT_SECURITY=true дефолтные пароли (change_me, admin123 и т.д.) вызывают отказ."""
    for weak in ["change_me", "secret", "admin123", "minioadmin"]:
        with patch.dict(os.environ, {
            "STRICT_SECURITY": "true",
            "REQUIRE_MTLS": "true",
            "REQUIRE_2FA": "true",
            "POSTGRES_PASSWORD": weak,
        }):
            with patch.object(settings, "REQUIRE_MTLS", True):
                with patch.object(settings, "REQUIRE_2FA", True):
                    with pytest.raises(RuntimeError, match="STRICT_SECURITY: insecure default password"):
                        settings.validate_production_env()


# --- SEC-11: Key ID (KID) и формат ENC2 ---

def test_sec11_enc2_with_kid_roundtrip():
    """Шифрование с указанием kid формирует заголовок ENC2:<kid>: и корректно расшифровывается."""
    key = b"K" * 32
    plaintext = b"Financial Secret Portfolio Balance: 100,000,000 RUB"
    
    # С Key ID (KID) -> формат ENC2
    encrypted = encrypt_data_at_rest(plaintext, key=key, kid="primary-2026")
    assert encrypted.startswith(b"ENC2:primary-2026:")
    decrypted = decrypt_data_at_rest(encrypted, key=key)
    assert decrypted == plaintext


def test_sec11_legacy_enc1_backward_compatibility():
    """Legacy пакеты формата ENC1 прозрачно расшифровываются."""
    key = b"L" * 32
    plaintext = b"Legacy encrypted data from previous version"
    
    # Без kid -> legacy ENC1
    legacy_enc = encrypt_data_at_rest(plaintext, key=key)
    assert legacy_enc.startswith(MAGIC_ENCRYPTION_V1)
    decrypted = decrypt_data_at_rest(legacy_enc, key=key)
    assert decrypted == plaintext


def test_sec11_chroma_envelope_enc2_and_enc1():
    """Chroma envelope поддерживает как ENC2 с kid, так и legacy ENC1."""
    key = b"C" * 32
    chunk = "Конфиденциальный нормативный акт Центробанка"
    
    # ENC2 с kid
    enc2 = encrypt_chroma_chunk(chunk, key=key, kid="rag-key-v2")
    assert is_encrypted_chunk(enc2) is True
    assert decrypt_chroma_chunk(enc2, key=key) == chunk
    
    # Legacy ENC1
    enc1 = encrypt_chroma_chunk(chunk, key=key)
    assert is_encrypted_chunk(enc1) is True
    assert decrypt_chroma_chunk(enc1, key=key) == chunk


# --- SEC-15: Маскирование CONTENTAI_PASSWORD ---

def test_sec15_contentai_password_masked():
    """Эндпоинт /api/config/contentai маскирует пароль как '********'."""
    client = TestClient(app)
    admin_tok = create_access_token({"sub": "admin"}, role=Role.SUPERADMIN)
    with patch.dict(os.environ, {"CONTENTAI_USERNAME": "test_user", "CONTENTAI_PASSWORD": "super_secret_password_123"}):
        resp = client.get("/api/config/contentai", headers={"Authorization": f"Bearer {admin_tok}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "test_user"
        assert data["password"] == "********"


# --- SEC-16: Обязательная проверка exp в JWT ---

def test_sec16_jwt_decode_requires_exp():
    """Токен без поля exp отклоняется библиотекой jwt с ошибкой MissingRequiredClaimError."""
    # Создаем токен без поля exp
    payload_no_exp = {
        "sub": "admin",
        "role": "superadmin",
        "type": "access",
        "jti": str(uuid.uuid4())
    }
    tok = jwt.encode(payload_no_exp, settings.PRIVATE_KEY, algorithm=settings.ALGORITHM)
    with pytest.raises(jwt.MissingRequiredClaimError):
        decode_access_token(tok)

    # Для токенов агентов токен без exp также возвращает None
    assert verify_agent_jwt(tok, settings.PUBLIC_KEY) is None


# --- SEC-26: Деривация ключей derive_key (PBKDF2 / HKDF / SHA-256) ---

def test_sec26_derive_key_methods():
    """derive_key поддерживает PBKDF2, HKDF и сохраняет совместимость с SHA-256."""
    passphrase = "Ultra-Secure-Master-Passphrase-2026"
    salt = b"test-custom-salt-12345"
    
    # 1. PBKDF2
    k_pbkdf2 = derive_key(passphrase, salt=salt, method="pbkdf2", iterations=1000)
    assert len(k_pbkdf2) == 32
    assert isinstance(k_pbkdf2, bytes)
    
    # 2. HKDF
    k_hkdf = derive_key(passphrase, salt=salt, method="hkdf")
    assert len(k_hkdf) == 32
    assert isinstance(k_hkdf, bytes)
    assert k_hkdf != k_pbkdf2
    
    # 3. Legacy SHA-256 совместимость
    import hashlib
    expected_sha = hashlib.sha256(passphrase.encode("utf-8")).digest()
    k_legacy = derive_key(passphrase, method="sha256")
    assert k_legacy == expected_sha
