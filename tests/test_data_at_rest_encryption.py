"""
Тесты для механизма шифрования данных при хранении (Data at Rest Encryption).
Проверяют криптографические примитивы AES-256-GCM, конвертное шифрование,
защиту от подделки (tamper detection), SQLAlchemy-тип EncryptedText,
конфигурацию MinIO SSE (SSE-S3, SSE-C) и настройки docker-compose.
"""

import os
import sys
import base64
import yaml
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Предварительный импорт fastapi до модификаций sys.path
import fastapi
from fastapi import Depends, HTTPException, status

# Настройка путей для импорта
sys.path.insert(0, os.path.abspath("src/admin_server"))
sys.path.insert(0, os.path.abspath("src/admin_server/fastapi"))
sys.path.insert(0, os.path.abspath("src/admin_server/vectors"))

# Обязательные переменные окружения для тестов
os.environ["POSTGRES_PASSWORD"] = "test_postgres_secret"

# Восстанавливаем реальный пакет minio, если он был замокан предыдущими тестами
for k in list(sys.modules.keys()):
    if k == "minio" or k.startswith("minio."):
        if isinstance(sys.modules[k], MagicMock):
            del sys.modules[k]
import minio
import minio.sse

# Изоляция зависимостей окружения
for mod in ["redis", "litellm", "asyncpg", "psycopg2", "psycopg2.pool", "chromadb", "chromadb.config", "langchain_text_splitters"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

import security
from security import (
    MAGIC_ENCRYPTION_V1,
    Settings,
    resolve_encryption_key,
    encrypt_data_at_rest,
    decrypt_data_at_rest,
    encrypt_text_at_rest,
    decrypt_text_at_rest,
    get_minio_sse,
)
from db.custom_types import EncryptedText


# --- ТЕСТЫ КРИПТОГРАФИЧЕСКИХ ПРИМИТИВОВ (AES-256-GCM) ---

def test_encrypt_decrypt_bytes_roundtrip():
    """Проверяет успешное шифрование и расшифровку байтовых данных разного размера."""
    key = b"0" * 32
    test_payloads = [
        b"",
        b"A",
        b"Financial Account Balance: 1500000.50 RUB",
        os.urandom(1024),      # 1 KB
        os.urandom(64 * 1024), # 64 KB
    ]
    for payload in test_payloads:
        ciphertext = encrypt_data_at_rest(payload, key=key)
        assert ciphertext.startswith(MAGIC_ENCRYPTION_V1)
        assert len(ciphertext) >= len(MAGIC_ENCRYPTION_V1) + 12 + len(payload) + 16
        decrypted = decrypt_data_at_rest(ciphertext, key=key)
        assert decrypted == payload


def test_encrypt_decrypt_text_roundtrip():
    """Проверяет успешное шифрование и расшифровку текстовых строк (UTF-8, кириллица, спецсимволы)."""
    key = "a" * 64  # Hex key
    texts = [
        "Тестовый финансовый договор № 123-ФЗ",
        "Confidential client metadata: {'id': 999, 'role': 'treasury'}",
        r"Специальные символы: !@#$%^&*()_+{}[]|\:;<>?,./~`'\" \n\t\r",
        "Emoji и мультиязычный текст: 💳 💰 🏦 Alpha & Omega",
    ]
    for text in texts:
        encrypted_b64 = encrypt_text_at_rest(text, key=key)
        assert isinstance(encrypted_b64, str)
        decrypted_text = decrypt_text_at_rest(encrypted_b64, key=key)
        assert decrypted_text == text


def test_associated_data_aead_validation():
    """Проверяет аутентифицированное шифрование с ассоциированными данными (AEAD AAD)."""
    key = b"K" * 32
    payload = b"Transfer 5,000,000 RUB from Acc1 to Acc2"
    aad = b"transaction-id-998877"

    # 1. Корректное совпадение AAD
    ciphertext = encrypt_data_at_rest(payload, key=key, associated_data=aad)
    decrypted = decrypt_data_at_rest(ciphertext, key=key, associated_data=aad)
    assert decrypted == payload

    # 2. Несовпадение AAD приводит к отказу расшифровки
    wrong_aad = b"transaction-id-000000"
    with pytest.raises(ValueError, match="Ошибка расшифровки"):
        decrypt_data_at_rest(ciphertext, key=key, associated_data=wrong_aad)

    # 3. Отсутствие AAD при расшифровке приводит к отказу
    with pytest.raises(ValueError, match="Ошибка расшифровки"):
        decrypt_data_at_rest(ciphertext, key=key, associated_data=None)


def test_tamper_detection_and_integrity():
    """Проверяет обнаружение любых модификаций шифротекста, тега аутентификации или заголовка."""
    key = b"Z" * 32
    original_data = b"Secret corporate balance sheet"
    ciphertext = bytearray(encrypt_data_at_rest(original_data, key=key))

    # 1. Повреждение заголовка MAGIC
    tampered_magic = bytearray(ciphertext)
    tampered_magic[0] = ord("X")
    with pytest.raises(ValueError, match="Неподдерживаемый формат"):
        decrypt_data_at_rest(bytes(tampered_magic), key=key)

    # 2. Повреждение случайного Nonce (байты 4-15)
    tampered_nonce = bytearray(ciphertext)
    tampered_nonce[5] ^= 0x01
    with pytest.raises(ValueError, match="Ошибка расшифровки"):
        decrypt_data_at_rest(bytes(tampered_nonce), key=key)

    # 3. Повреждение тела шифротекста
    tampered_body = bytearray(ciphertext)
    tampered_body[20] ^= 0x01
    with pytest.raises(ValueError, match="Ошибка расшифровки"):
        decrypt_data_at_rest(bytes(tampered_body), key=key)

    # 4. Повреждение тега аутентификации (последние 16 байт)
    tampered_tag = bytearray(ciphertext)
    tampered_tag[-1] ^= 0xFF
    with pytest.raises(ValueError, match="Ошибка расшифровки"):
        decrypt_data_at_rest(bytes(tampered_tag), key=key)

    # 5. Усеченный пакет
    with pytest.raises(ValueError, match="длина пакета меньше минимально допустимой"):
        decrypt_data_at_rest(b"ENC11234", key=key)

    # 6. Некорректный тип входных данных
    with pytest.raises(TypeError):
        encrypt_data_at_rest(12345, key=key)  # type: ignore
    with pytest.raises(TypeError):
        decrypt_data_at_rest("not_bytes", key=key)  # type: ignore


def test_decrypt_with_wrong_key_fails():
    """Проверяет невозможность расшифровки данных чужим ключом."""
    key_alice = b"A" * 32
    key_bob = b"B" * 32
    ciphertext = encrypt_data_at_rest(b"Confidential financial statement", key=key_alice)

    with pytest.raises(ValueError, match="Ошибка расшифровки"):
        decrypt_data_at_rest(ciphertext, key=key_bob)


def test_resolve_encryption_key_formats():
    """Проверяет поддержку всех форматов ключа: raw bytes, hex, base64, passphrase, fallback."""
    # 1. Raw 32 bytes
    k_bytes = b"1" * 32
    assert resolve_encryption_key(k_bytes) == k_bytes

    # Некорректный размер байтов
    with pytest.raises(ValueError, match="должен быть ровно 32 байта"):
        resolve_encryption_key(b"too_short")

    # 2. Hex 64 символа
    k_hex = "01" * 32
    res_hex = resolve_encryption_key(k_hex)
    assert len(res_hex) == 32
    assert res_hex == bytes.fromhex(k_hex)

    # 3. Base64 строка
    k_b64 = base64.b64encode(b"B" * 32).decode("ascii")
    assert resolve_encryption_key(k_b64) == b"B" * 32

    # 4. Произвольная кодовая фраза (деривация через SHA-256)
    k_pass = "Enterprise-Master-Passphrase-2026"
    import hashlib
    assert resolve_encryption_key(k_pass) == hashlib.sha256(k_pass.encode("utf-8")).digest()

    # 5. Пустая строка вызывает ошибку
    with pytest.raises(ValueError, match="не может быть пустой"):
        resolve_encryption_key("")

    # 6. Некорректный тип вызывает TypeError
    with pytest.raises(TypeError):
        resolve_encryption_key(12345)  # type: ignore


# --- ТЕСТЫ SQLALCHEMY ENCRYPTEDTEXT ТИПА ---

def test_sqlalchemy_encrypted_text_type_decorator():
    """Проверяет работу SQLAlchemy TypeDecorator EncryptedText: шифрование, чтение и обратную совместимость."""
    enc_type = EncryptedText()

    # 1. Шифрование при записи (bind_param)
    plain_val = "Секретный номер банковского счёта 40817810000000001234"
    bound_val = enc_type.process_bind_param(plain_val, None)
    assert isinstance(bound_val, str)
    assert bound_val != plain_val
    # Проверяем, что результат декодируется как наш ENC1 envelope
    raw_dec = base64.urlsafe_b64decode(bound_val.encode("ascii"))
    assert raw_dec.startswith(MAGIC_ENCRYPTION_V1)

    # 2. Расшифровка при чтении (result_value)
    read_val = enc_type.process_result_value(bound_val, None)
    assert read_val == plain_val

    # 3. Обратная совместимость с ранее не зашифрованными данными
    legacy_val = "Устаревшая запись в открытом виде"
    read_legacy = enc_type.process_result_value(legacy_val, None)
    assert read_legacy == legacy_val

    # 4. Обработка None
    assert enc_type.process_bind_param(None, None) is None
    assert enc_type.process_result_value(None, None) is None


# --- ТЕСТЫ MINIO SERVER-SIDE ENCRYPTION (SSE) ---

def test_minio_sse_configuration_modes():
    """Проверяет генерацию параметров ServerSideEncryption для MinIO client."""
    for k in list(sys.modules.keys()):
        if k == "minio" or k.startswith("minio."):
            if isinstance(sys.modules[k], MagicMock):
                del sys.modules[k]
    import minio.sse
    from minio.sse import SseS3, SseCustomerKey

    # 1. Принудительный режим SSE-S3
    sse_s3 = get_minio_sse(sse_type="s3")
    assert isinstance(sse_s3, SseS3) or sse_s3.headers() == {"X-Amz-Server-Side-Encryption": "AES256"}
    assert sse_s3.headers() == {"X-Amz-Server-Side-Encryption": "AES256"}

    # 2. Принудительный режим SSE-C с кастомным ключом
    cust_key = b"C" * 32
    sse_c = get_minio_sse(sse_type="ssec", customer_key=cust_key)
    assert isinstance(sse_c, SseCustomerKey) or "X-Amz-Server-Side-Encryption-Customer-Key" in sse_c.headers()
    c_headers = sse_c.headers()
    assert c_headers["X-Amz-Server-Side-Encryption-Customer-Algorithm"] == "AES256"
    assert "X-Amz-Server-Side-Encryption-Customer-Key" in c_headers

    # 3. Отключенный режим
    assert get_minio_sse(sse_type="none") is None
    assert get_minio_sse(sse_type="disabled") is None


def test_vector_worker_ssec_helper():
    """Проверяет вспомогательную функцию get_minio_ssec в модуле vector_worker."""
    from vectors import vector_worker

    # При отсутствии ключа возвращается None
    with patch.dict(os.environ, {"MINIO_SSE_C_KEY": ""}, clear=False):
        assert vector_worker.get_minio_ssec() is None

    # При заданном ключе возвращается объект SseCustomerKey
    with patch.dict(os.environ, {"MINIO_SSE_C_KEY": "a" * 64}, clear=False):
        ssec = vector_worker.get_minio_ssec()
        assert ssec is not None
        assert "X-Amz-Server-Side-Encryption-Customer-Key" in ssec.headers()


# --- ТЕСТЫ DOCKER-COMPOSE И ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ---

def test_docker_compose_encryption_configuration():
    """Проверяет наличие настроек Data at Rest и KMS в файле docker-compose.yml."""
    compose_path = Path(__file__).parent.parent / "docker-compose.yml"
    assert compose_path.exists(), "docker-compose.yml не найден"

    with open(compose_path, "r", encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    services = compose["services"]

    # 1. Проверка MinIO KMS
    minio_env = services["minio"]["environment"]
    assert any("MINIO_KMS_SECRET_KEY" in env for env in minio_env), "MinIO должен содержать MINIO_KMS_SECRET_KEY"

    # 2. Проверка Admin Server
    admin_env = services["admin-server"]["environment"]
    assert any("DATA_ENCRYPTION_KEY" in env for env in admin_env), "admin-server должен содержать DATA_ENCRYPTION_KEY"
    assert any("MINIO_SSE_ENABLED" in env for env in admin_env), "admin-server должен содержать MINIO_SSE_ENABLED"

    # 3. Проверка Vector Worker
    worker_env = services["vector-worker"]["environment"]
    assert any("DATA_ENCRYPTION_KEY" in env for env in worker_env), "vector-worker должен содержать DATA_ENCRYPTION_KEY"
    assert any("MINIO_SSE_C_KEY" in env for env in worker_env), "vector-worker должен содержать MINIO_SSE_C_KEY"


def test_validate_production_env_requires_data_encryption_key():
    """Проверяет, что validate_production_env() требует DATA_ENCRYPTION_KEY для прома."""
    settings = Settings()
    
    # Сохраняем исходное окружение
    saved_env = {k: os.environ.get(k) for k in [
        "JWT_PRIVATE_KEY", "JWT_PRIVATE_KEY_PATH", "ADMIN_TOTP_SECRET",
        "AGENT_DIGITAL_BOOTSTRAP_TOKEN", "AGENT_BANK_BOOTSTRAP_TOKEN",
        "AGENT_INVEST_BOOTSTRAP_TOKEN", "AGENT_MAIN_BOOTSTRAP_TOKEN",
        "DATA_ENCRYPTION_KEY"
    ]}

    try:
        # Устанавливаем все ключи кроме DATA_ENCRYPTION_KEY
        os.environ["JWT_PRIVATE_KEY"] = settings.PRIVATE_KEY
        os.environ["ADMIN_TOTP_SECRET"] = "JBSWY3DPEHPK3PXP"
        for a in ["DIGITAL", "BANK", "INVEST", "MAIN"]:
            os.environ[f"AGENT_{a}_BOOTSTRAP_TOKEN"] = "token"
        os.environ.pop("DATA_ENCRYPTION_KEY", None)
        settings.DATA_ENCRYPTION_KEY = ""

        with pytest.raises(RuntimeError) as exc_info:
            settings.validate_production_env()
        assert "DATA_ENCRYPTION_KEY" in str(exc_info.value)
    finally:
        for k, v in saved_env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)


def test_upload_document_passes_minio_sse():
    """Проверяет передачу параметра sse в minio_client.put_object при выгрузке документа в /api/v1/rag/documents."""
    import sys
    import time
    import uuid
    import json
    import base64
    import tempfile
    import datetime
    from pathlib import Path
    from unittest.mock import MagicMock, patch
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    from fastapi.testclient import TestClient
    from server import app, minio_client
    from security import compute_rag_canonical_digest, set_trusted_root_ca

    certs_dir = Path(tempfile.gettempdir()) / "fin_ai_test_pki_certs"
    ca_pem = (certs_dir / "ca.crt").read_text(encoding="utf-8")
    ca_key = serialization.load_pem_private_key((certs_dir / "ca.key").read_bytes(), password=None)
    ca_cert = x509.load_pem_x509_certificate(ca_pem.encode("utf-8"))
    set_trusted_root_ca(ca_pem)

    # Выпускаем валидный сертификат с ролью admin_operator, подписанный Root CA
    client_key = rsa.generate_private_key(65537, 2048, default_backend())
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    client_subject = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Financial AI Test"),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "admin_operator"),
        x509.NameAttribute(NameOID.COMMON_NAME, "superadmin-workstation"),
    ])
    client_cert = (
        x509.CertificateBuilder()
        .subject_name(client_subject)
        .issuer_name(ca_cert.subject)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now_dt - datetime.timedelta(days=1))
        .not_valid_after(now_dt + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256(), default_backend())
    )
    client_cert_pem = client_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")

    minio_client.put_object = MagicMock()
    minio_client.bucket_exists = MagicMock(return_value=True)

    mock_sse_obj = MagicMock()
    now_ts = int(time.time())
    nonce = f"test_nonce_sse_{uuid.uuid4().hex[:8]}"
    doc_data = {
        "document_title": "Test SSE Policy",
        "collection_name": "knowledge-main",
        "content": "# Test content for SSE verification\nSome text here.",
        "metadata": {"short_name": "sse_test"}
    }
    body_bytes = json.dumps(doc_data, ensure_ascii=False).encode("utf-8")
    canonical = compute_rag_canonical_digest(now_ts, nonce, body_bytes)
    sig = client_key.sign(canonical, padding.PKCS1v15(), hashes.SHA256())
    sig_b64 = base64.b64encode(sig).decode("ascii")

    headers = {
        "X-Timestamp": str(now_ts),
        "X-Nonce": nonce,
        "X-Signature": sig_b64,
        "X-Cert": client_cert_pem,
        "Content-Type": "application/json",
        "X-Client-Cert-Verify": "SUCCESS",
        "X-Client-Cert-Subject": "CN=superadmin-workstation,OU=admin_operator"
    }

    rag_mod = sys.modules.get("routers.rag_documents") or sys.modules.get("src.admin_server.fastapi.routers.rag_documents")
    with patch.object(rag_mod, "get_minio_sse", return_value=mock_sse_obj):
        client = TestClient(app)
        response = client.post("/api/v1/rag/documents", content=body_bytes, headers=headers)
        assert response.status_code == 201
        minio_client.put_object.assert_called_once()
        _, kwargs = minio_client.put_object.call_args
        assert kwargs.get("sse") == mock_sse_obj

