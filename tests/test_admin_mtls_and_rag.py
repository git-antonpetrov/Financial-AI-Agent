"""
Тесты для Фазы 3: Защита Сервера Администратора (Zero-Trust mTLS, CRL, Nonce Replay Protection,
проверка цифровой подписи X-Signature и эндпоинт приема документов в RAG POST /api/v1/rag/documents).
"""

import os
import sys
import json
import time
import uuid
import base64
import hashlib
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

from fastapi import HTTPException
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric import rsa, ec, padding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography import x509
from cryptography.x509.oid import NameOID
import datetime

# Моки внешних сервисов
for mod in ["redis", "minio", "litellm", "asyncpg", "chromadb", "psycopg2"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

# Настройка путей
sys.path.insert(0, os.path.abspath("src/admin_server"))
sys.path.insert(0, os.path.abspath("src/admin_server/fastapi"))

import security
from security import (
    settings,
    verify_mtls_client_identity,
    verify_mtls_client_certificate,
    parse_mtls_client_certificate,
    revoke_certificate_in_redis,
    is_certificate_revoked_in_redis,
    record_admin_nonce_if_new,
    clear_admin_nonces_cache,
    compute_rag_canonical_digest,
    verify_digital_signature,
    sign_server_receipt,
    calculate_key_fingerprint,
    _revoked_certs_memory,
)
import server
from server import app
from db import models, crud
from db.database import get_db


# --- Хелперы для генерации тестовых ключей и сертификатов ---

@pytest.fixture(scope="module")
def client_keypair():
    """Генерирует тестовую пару ключей RSA-2048 для клиента."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode("utf-8")
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("utf-8")
    return priv, priv_pem, pub_pem


@pytest.fixture(scope="module")
def client_x509_cert(client_keypair):
    """Генерирует тестовый X.509 сертификат клиента для mTLS."""
    priv, _, pub_pem = client_keypair
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "FinancialAI"),
        x509.NameAttribute(NameOID.COMMON_NAME, "superadmin-workstation"),
    ])
    cert = x509.CertificateBuilder().subject_name(
        subject
    ).issuer_name(
        issuer
    ).public_key(
        priv.public_key()
    ).serial_number(
        100500
    ).not_valid_before(
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    ).not_valid_after(
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
    ).sign(priv, hashes.SHA256())

    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    return cert, cert_pem, "100500"


@pytest.fixture(autouse=True)
def clean_test_state():
    """Очищает кэши nonces и отзывов перед каждым тестом."""
    clear_admin_nonces_cache()
    _revoked_certs_memory.clear()
    yield
    clear_admin_nonces_cache()
    _revoked_certs_memory.clear()


# --- 1. ТЕСТЫ БАЗЫ ОТЗЫВОВ СЕРТИФИКАТОВ (CRL / REVOCATION) ---

def test_certificate_revocation_lifecycle():
    """Проверяет добавление сертификата в черный список и проверку статуса отзыва."""
    serial = "99887766"
    fingerprint = "sha256:aabbccddeeff"

    assert not is_certificate_revoked_in_redis(serial=serial)
    assert not is_certificate_revoked_in_redis(fingerprint=fingerprint)

    # Отзываем по серийному номеру
    revoke_certificate_in_redis(serial=serial)
    assert is_certificate_revoked_in_redis(serial=serial) is True
    assert is_certificate_revoked_in_redis(serial="11111111") is False

    # Отзываем по отпечатку
    revoke_certificate_in_redis(fingerprint=fingerprint)
    assert is_certificate_revoked_in_redis(fingerprint=fingerprint) is True
    assert is_certificate_revoked_in_redis(fingerprint="sha256:00000000") is False


def test_mtls_blocks_revoked_certificate():
    """Проверяет, что отозванный сертификат блокируется валидатором идентичности mTLS."""
    revoked_serial = "88884444"
    revoke_certificate_in_redis(serial=revoked_serial)

    from fastapi import Request
    scope = {
        "type": "http",
        "headers": [
            (b"x-client-cert-verify", b"SUCCESS"),
            (b"x-client-cert-subject", b"CN=superadmin-workstation,O=FinancialAI"),
            (b"x-client-cert-serial", revoked_serial.encode("utf-8")),
        ]
    }
    req = Request(scope)

    with pytest.raises(HTTPException) as exc:
        verify_mtls_client_identity(req)
    assert exc.value.status_code == 401
    assert "отозван" in exc.value.detail or "revoked" in exc.value.detail.lower()


# --- 2. ТЕСТЫ ЗАЩИТЫ ОТ REPLAY-АТАК (NONCE PROTECTION) ---

def test_nonce_store_replay_prevention():
    """Проверяет, что повторное использование Nonce немедленно отклоняется."""
    nonce = f"test-nonce-{uuid.uuid4()}"

    # Первое использование - успешно
    assert record_admin_nonce_if_new(nonce) is True

    # Второе использование того же nonce (Replay Attack) - заблокировано
    assert record_admin_nonce_if_new(nonce) is False

    # Пустой или пробельный nonce - отклоняется
    assert record_admin_nonce_if_new("") is False
    assert record_admin_nonce_if_new("   ") is False

    # Другой nonce - разрешен
    another_nonce = f"test-nonce-{uuid.uuid4()}"
    assert record_admin_nonce_if_new(another_nonce) is True


# --- 3. ТЕСТЫ ЦИФРОВЫХ ПОДПИСЕЙ И КВИТАНЦИЙ СЕРВЕРА ---

def test_verify_digital_signature_rsa_pss(client_keypair):
    """Проверяет корректность подписи и валидации по алгоритму RSA-PSS."""
    priv, _, pub_pem = client_keypair
    payload = b"test payload for zero-trust verification"

    # Подпись RSA-PSS
    sig_raw = priv.sign(
        payload,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256()
    )
    sig_b64 = base64.b64encode(sig_raw).decode("ascii")

    # Валидация подписи
    assert verify_digital_signature(pub_pem, sig_b64, payload) is True

    # Модификация полезной нагрузки должна приводить к сбою валидации
    assert verify_digital_signature(pub_pem, sig_b64, b"tampered payload") is False

    # Поврежденная подпись
    assert verify_digital_signature(pub_pem, "invalid_sig_base64", payload) is False


def test_verify_digital_signature_pkcs1v15_and_x509(client_keypair, client_x509_cert):
    """Проверяет валидацию подписи PKCS#1 v1.5 (Windows Hello / TPM) и работу с X.509 сертификатом."""
    priv, _, _ = client_keypair
    _, cert_pem, _ = client_x509_cert
    payload = b"hardware-backed tpm 2.0 biometric sign request"

    # Подпись PKCS#1 v1.5
    sig_raw = priv.sign(payload, padding.PKCS1v15(), hashes.SHA256())
    sig_b64 = base64.b64encode(sig_raw).decode("ascii")

    # Валидация по X.509 PEM сертификату
    assert verify_digital_signature(cert_pem, sig_b64, payload) is True


def test_sign_server_receipt():
    """Проверяет подписание квитанции сервером и соответствие цифровой подписи."""
    receipt_data = {
        "receipt_id": str(uuid.uuid4()),
        "status": "QUEUED_FOR_INDEXING",
        "document_id": "test_hash_123",
        "timestamp": int(time.time()),
        "nonce": "nonce-abc"
    }

    sig_b64, fp = sign_server_receipt(receipt_data)
    assert sig_b64 and len(sig_b64) > 50
    assert fp.startswith("sha256:")

    # Проверяем подпись квитанции публичным ключом сервера
    canonical_receipt = json.dumps(receipt_data, sort_keys=True, ensure_ascii=False).encode("utf-8")
    assert verify_digital_signature(settings.PUBLIC_KEY, sig_b64, canonical_receipt) is True


# --- 4. ИНТЕГРАЦИОННЫЕ ТЕСТЫ ЭНДПОИНТА POST /api/v1/rag/documents ---

class MockDbSession:
    """Имитатор AsyncSession для моделей Document и AuditLog в памяти."""
    def __init__(self):
        self.documents = []
        self.logs = []

    def get_bind(self):
        return None

    def add(self, entry):
        if isinstance(entry, models.AuditLog):
            if not getattr(entry, "id", None):
                entry.id = len(self.logs) + 1
            self.logs.append(entry)
        else:
            if not getattr(entry, "id", None):
                entry.id = len(self.documents) + 1
            self.documents.append(entry)

    async def commit(self):
        pass

    async def refresh(self, entry):
        pass

    async def execute(self, statement):
        class ScalarResult:
            def __init__(self, items):
                self._items = items
            def scalars(self):
                return self
            def all(self):
                return list(self._items)
            def first(self):
                return self._items[0] if self._items else None
            def scalar_one_or_none(self):
                return self._items[0] if self._items else None

        return ScalarResult(list(self.logs))


@pytest.fixture
def mock_db_session():
    """Создает сессию базы данных в памяти."""
    return MockDbSession()


def test_submit_rag_document_success(client_keypair, client_x509_cert, mock_db_session):
    """
    Проверяет успешный сквозной сценарий добавления документа в RAG:
    - Клиент подписывает запрос аппаратным ключом;
    - Сервер валидирует подпись, Nonce, Timestamp;
    - Возвращает статус 201 Created и подписанный AckReceipt.
    """
    priv, _, _ = client_keypair
    _, cert_pem, _ = client_x509_cert

    app.dependency_overrides[get_db] = lambda: mock_db_session

    try:
        client = TestClient(app)

        body_dict = {
            "document_title": "Анализ ликвидности банка Q3 2026",
            "collection_name": "knowledge-main",
            "content": "# Отчет\nНорматив краткосрочной ликвидности Н26 соблюден в объеме 145%.",
            "metadata": {"source": "admin_workstation", "classification": "restricted"}
        }
        body_bytes = json.dumps(body_dict, ensure_ascii=False).encode("utf-8")

        now_ts = int(time.time())
        nonce = f"nonce-{uuid.uuid4()}"

        canonical_bytes = compute_rag_canonical_digest(
            timestamp=now_ts,
            nonce=nonce,
            body_bytes=body_bytes
        )

        # Подпись RSA-PSS
        sig_raw = priv.sign(
            canonical_bytes,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256()
        )
        sig_b64 = base64.b64encode(sig_raw).decode("ascii")

        headers = {
            "Content-Type": "application/json",
            "X-Signature": sig_b64,
            "X-Nonce": nonce,
            "X-Timestamp": str(now_ts),
            "X-Cert": cert_pem,
            "X-Client-Cert-Verify": "SUCCESS",
            "X-Client-Cert-Subject": "CN=superadmin-workstation,O=FinancialAI",
            "X-Client-Cert-Serial": "100500",
        }

        resp = client.post("/api/v1/rag/documents", content=body_bytes, headers=headers)
        assert resp.status_code == 201
        data = resp.json()

        assert data["status"] == "QUEUED_FOR_INDEXING"
        assert data["document_title"] == "Анализ ликвидности банка Q3 2026"
        assert data["collection_name"] == "knowledge-main"
        assert data["nonce"] == nonce
        assert data["signature"] is not None
        assert data["server_key_fingerprint"].startswith("sha256:")

        # Проверка заголовков ответа
        assert resp.headers.get("X-Server-Signature") == data["signature"]
        # Проверка сохранения документа в БД
        assert len(mock_db_session.documents) > 0
        saved_doc = mock_db_session.documents[0]
        assert saved_doc.system_name == "Анализ ликвидности банка Q3 2026"
        assert saved_doc.agent_name == "main"
        assert saved_doc.raw_content is not None

    finally:
        app.dependency_overrides.pop(get_db, None)


def test_submit_rag_document_replay_attack_rejected(client_keypair, client_x509_cert, mock_db_session):
    """Проверяет отклонение повторного запроса с тем же Nonce (код 409 Conflict)."""
    priv, _, _ = client_keypair
    _, cert_pem, _ = client_x509_cert

    app.dependency_overrides[get_db] = lambda: mock_db_session

    try:
        client = TestClient(app)

        body_dict = {"document_title": "Doc1", "content": "Content"}
        body_bytes = json.dumps(body_dict).encode("utf-8")
        now_ts = int(time.time())
        nonce = f"replay-test-nonce-{uuid.uuid4()}"

        canonical = compute_rag_canonical_digest(now_ts, nonce, body_bytes)
        sig_raw = priv.sign(
            canonical,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256()
        )
        sig_b64 = base64.b64encode(sig_raw).decode("ascii")

        headers = {
            "Content-Type": "application/json",
            "X-Signature": sig_b64,
            "X-Nonce": nonce,
            "X-Timestamp": str(now_ts),
            "X-Cert": cert_pem,
        }

        # 1-й запрос успешен
        resp1 = client.post("/api/v1/rag/documents", content=body_bytes, headers=headers)
        assert resp1.status_code == 201

        # 2-й запрос с тем же Nonce отклоняется с 409 Conflict
        resp2 = client.post("/api/v1/rag/documents", content=body_bytes, headers=headers)
        assert resp2.status_code == 409
        assert "Replay attack" in resp2.json()["detail"]

    finally:
        app.dependency_overrides.pop(get_db, None)


def test_submit_rag_document_expired_timestamp_rejected(client_keypair, client_x509_cert, mock_db_session):
    """Проверяет отклонение запроса с устаревшей временной меткой (>300с, код 400 Bad Request)."""
    priv, _, _ = client_keypair
    _, cert_pem, _ = client_x509_cert

    app.dependency_overrides[get_db] = lambda: mock_db_session

    try:
        client = TestClient(app)

        body_dict = {"document_title": "Stale Doc", "content": "Stale Content"}
        body_bytes = json.dumps(body_dict).encode("utf-8")
        stale_ts = int(time.time()) - 500  # 500 секунд назад
        nonce = f"stale-nonce-{uuid.uuid4()}"

        canonical = compute_rag_canonical_digest(stale_ts, nonce, body_bytes)
        sig_raw = priv.sign(
            canonical,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256()
        )
        sig_b64 = base64.b64encode(sig_raw).decode("ascii")

        headers = {
            "Content-Type": "application/json",
            "X-Signature": sig_b64,
            "X-Nonce": nonce,
            "X-Timestamp": str(stale_ts),
            "X-Cert": cert_pem,
        }

        resp = client.post("/api/v1/rag/documents", content=body_bytes, headers=headers)
        assert resp.status_code == 400
        assert "истек" in resp.json()["detail"] or "expired" in resp.json()["detail"].lower()

    finally:
        app.dependency_overrides.pop(get_db, None)


def test_submit_rag_document_invalid_signature_rejected(client_keypair, client_x509_cert, mock_db_session):
    """Проверяет отклонение запроса с поддельной цифровой подписью (код 401 Unauthorized)."""
    _, _, _ = client_keypair
    _, cert_pem, _ = client_x509_cert

    # Генерируем ключ злоумышленника
    fake_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    app.dependency_overrides[get_db] = lambda: mock_db_session

    try:
        client = TestClient(app)

        body_dict = {"document_title": "Fake Doc", "content": "Malicious Content"}
        body_bytes = json.dumps(body_dict).encode("utf-8")
        now_ts = int(time.time())
        nonce = f"fake-sig-nonce-{uuid.uuid4()}"

        canonical = compute_rag_canonical_digest(now_ts, nonce, body_bytes)
        # Подписываем ключом злоумышленника, а в X-Cert передаем сертификат администратора
        sig_raw = fake_priv.sign(
            canonical,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256()
        )
        sig_b64 = base64.b64encode(sig_raw).decode("ascii")

        headers = {
            "Content-Type": "application/json",
            "X-Signature": sig_b64,
            "X-Nonce": nonce,
            "X-Timestamp": str(now_ts),
            "X-Cert": cert_pem,
        }

        resp = client.post("/api/v1/rag/documents", content=body_bytes, headers=headers)
        assert resp.status_code == 401
        assert "подпись" in resp.json()["detail"].lower() or "signature" in resp.json()["detail"].lower()

    finally:
        app.dependency_overrides.pop(get_db, None)
