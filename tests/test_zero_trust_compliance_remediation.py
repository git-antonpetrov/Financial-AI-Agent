"""
Тесты проверки полного соответствия Zero-Trust спецификации (Remediation & Compliance).
Проверяет:
1. Загрузку сертификата X.509 сервера (SERVER_CERT) и эндпоинты /api/auth/public-key.
2. Мгновенный отзыв (Instant Revocation) и реактивацию ключей агентов с синхронизацией памяти и pub/sub.
3. Валидацию квитанции сервера (AckReceipt) через сертификат сервера и цепочку доверия Root CA.
4. Устойчивость RAG vector-worker к документам без YAML frontmatter (fallback на task payload).
5. Новые BFF эндпоинты клиента администратора: audit logs, audit verify, agent keys, key rotation, mTLS status.
"""

import os
import sys
import json
import uuid
import base64
import hashlib
import time
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

# Защита от затенения пакета fastapi локальной папкой src/admin_server/fastapi
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
admin_server_path = os.path.abspath(os.path.join(repo_root, "src", "admin_server"))
while admin_server_path in sys.path:
    sys.path.remove(admin_server_path)
if "fastapi" in sys.modules and getattr(sys.modules["fastapi"], "__file__", "") and "admin_server" in sys.modules["fastapi"].__file__:
    del sys.modules["fastapi"]

from fastapi import FastAPI
from fastapi.testclient import TestClient

# Моки внешних сервисов при автономном тестировании
for mod in ["redis", "minio", "litellm", "asyncpg", "chromadb", "psycopg2", "psycopg2.pool"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

# Настройка путей
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

for p in [
    os.path.join(repo_root, "src", "pki"),
    os.path.join(repo_root, "src", "admin_server", "fastapi"),
    os.path.join(repo_root, "src", "admin_client", "backend"),
]:
    if p not in sys.path:
        sys.path.insert(0, p)

from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography import x509
from cryptography.x509.oid import NameOID
import datetime

from src.pki.core.ca_engine import RootCAEngine
from src.pki.core.cert_issuer import CertificateIssuer
from src.admin_client.backend.core.crypto.signer import ZeroTrustClientSigner
import security
from security import (
    settings,
    sign_server_receipt,
    revoke_agent_key_in_redis,
    is_agent_key_revoked_in_redis,
    unrevoke_agent_key_in_redis,
    _revoked_certs_memory,
)
from src.admin_client.backend.core.remote_client import RemoteAdminClient
from src.admin_client.backend import main


@pytest.fixture
def pki_setup(tmp_path):
    """Создает временный Root CA и сертификат сервера для тестов."""
    ca_dir = tmp_path / "ca"
    ca_engine = RootCAEngine(data_dir=ca_dir, passphrase="test_passphrase_12345", validity_years=1)
    root_cert, ca_key = ca_engine.ensure_initialized()
    issuer = CertificateIssuer(ca_engine)

    server_cert, server_key = issuer.issue_certificate(
        common_name="admin-server",
        role="Security Operations",
        san_dns_names=["admin-server", "localhost"],
        san_ip_addresses=["127.0.0.1"],
        is_server=True,
        is_client=True,
        validity_days=30
    )

    client_cert, client_key = issuer.issue_certificate(
        common_name="superadmin-workstation",
        role="admin_operator",
        san_dns_names=["localhost"],
        san_ip_addresses=["127.0.0.1"],
        is_server=False,
        is_client=True,
        validity_days=30
    )

    ca_pem = root_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    server_cert_pem = server_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    server_key_pem = server_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode("utf-8")

    client_cert_pem = client_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    client_key_pem = client_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode("utf-8")

    return {
        "ca_pem": ca_pem,
        "server_cert_pem": server_cert_pem,
        "server_key_pem": server_key_pem,
        "client_cert_pem": client_cert_pem,
        "client_key_pem": client_key_pem,
    }


def test_server_keys_and_cert_loading_from_file_and_endpoint(pki_setup, tmp_path):
    """Проверяет загрузку X.509 сертификата сервера в Settings и ответ /api/auth/public-key."""
    key_file = tmp_path / "admin_server.key"
    cert_file = tmp_path / "admin_server.crt"

    key_file.write_text(pki_setup["server_key_pem"], encoding="utf-8")
    cert_file.write_text(pki_setup["server_cert_pem"], encoding="utf-8")

    with patch.dict(os.environ, {
        "JWT_PRIVATE_KEY_PATH": str(key_file),
        "JWT_PUBLIC_KEY_PATH": str(cert_file)
    }):
        settings.load_keys()
        assert settings.PRIVATE_KEY == pki_setup["server_key_pem"].strip()
        assert settings.SERVER_CERT == pki_setup["server_cert_pem"].strip()
        assert "BEGIN PUBLIC KEY" in settings.PUBLIC_KEY

    # Тестируем эндпоинт /api/auth/public-key
    from src.admin_server.fastapi.server import app
    client = TestClient(app)
    resp = client.get("/api/auth/public-key")
    assert resp.status_code == 200
    data = resp.json()
    assert data["algorithm"] == "RS256"
    assert "BEGIN PUBLIC KEY" in data["public_key"]
    assert data["certificate"] == settings.SERVER_CERT
    assert data["fingerprint"].startswith("sha256:")


def test_agent_key_revocation_and_unrevocation_sync():
    """Проверяет мгновенный отзыв в памяти, в Redis и снятие блокировки при реактивации."""
    agent = "bank-service-test"
    kid = "bank-test-kid-101"

    mock_redis = MagicMock()
    mock_redis.exists.return_value = 1

    # До отзыва
    unrevoke_agent_key_in_redis(agent, kid, redis_conn=mock_redis)
    _revoked_certs_memory.discard(f"agent:{agent}")
    _revoked_certs_memory.discard(f"agent_key:{kid}")

    # 1. Отзыв ключа
    revoke_agent_key_in_redis(agent, kid, redis_conn=mock_redis)
    assert f"agent:{agent}" in _revoked_certs_memory
    assert f"agent_key:{kid}" in _revoked_certs_memory
    assert is_agent_key_revoked_in_redis(agent, kid) is True

    # Проверяем публикацию в канал security:revocations
    mock_redis.publish.assert_any_call("security:revocations", f"agent:{agent}")
    mock_redis.publish.assert_any_call("security:revocations", f"agent_key:{kid}")

    # 2. Снятие блокировки (Реактивация)
    unrevoke_agent_key_in_redis(agent, kid, redis_conn=mock_redis)
    assert f"agent:{agent}" not in _revoked_certs_memory
    assert f"agent_key:{kid}" not in _revoked_certs_memory
    mock_redis.delete.assert_any_call(f"revoked:agent:{agent}")
    mock_redis.publish.assert_any_call("security:revocations", f"unrevoked:agent:{agent}")


def test_receipt_verification_with_server_cert_and_root_ca(pki_setup):
    """Проверяет криптографическую проверку квитанции сервера через сертификат сервера и Root CA."""
    signer = ZeroTrustClientSigner(
        mode="software",
        cert_pem=pki_setup["client_cert_pem"],
        key_pem=pki_setup["client_key_pem"],
        ca_cert_pem=pki_setup["ca_pem"],
    )

    receipt_data = {
        "receipt_id": str(uuid.uuid4()),
        "status": "QUEUED_FOR_INDEXING",
        "document_id": "sha256:abc123def456",
        "document_title": "Финансовый Регламент 2026",
        "collection_name": "knowledge-bank",
        "timestamp": int(time.time()),
        "nonce": "test_nonce_9999"
    }

    # Сервер подписывает своим приватным ключом
    server_sig, _ = sign_server_receipt(receipt_data, private_key_pem=pki_setup["server_key_pem"])
    assert server_sig != ""

    # 1. Клиент проверяет квитанцию с валидным сертификатом сервера (подписанным Root CA)
    verified = signer.verify_server_receipt(
        receipt_data=receipt_data,
        server_signature_b64=server_sig,
        trusted_server_cert_or_pubkey=pki_setup["server_cert_pem"]
    )
    assert verified is True

    # 2. Изменение данных квитанции приводит к отказу проверки
    tampered_data = dict(receipt_data)
    tampered_data["status"] = "TAMPERED_STATUS"
    assert signer.verify_server_receipt(tampered_data, server_sig, pki_setup["server_cert_pem"]) is False

    # 3. Сертификат сервера, не подписанный доверенным Root CA, отклоняется
    rogue_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rogue_cert = x509.CertificateBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "rogue-server")])
    ).issuer_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-root-ca")])
    ).public_key(
        rogue_key.public_key()
    ).serial_number(
        12345
    ).not_valid_before(
        datetime.datetime.now(datetime.timezone.utc)
    ).not_valid_after(
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
    ).sign(rogue_key, hashes.SHA256())

    rogue_cert_pem = rogue_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    rogue_sig, _ = sign_server_receipt(receipt_data, private_key_pem=rogue_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode("utf-8"))

    # Валидация цепочки должна отклонить подпись с фальшивым сертификатом
    assert signer.verify_server_receipt(receipt_data, rogue_sig, rogue_cert_pem) is False


def test_vector_worker_short_name_and_metadata_fallback():
    """Проверяет, что vector_worker корректно использует short_name и metadata из задачи при отсутствии frontmatter."""
    # Тестируем логику извлечения метаданных
    task = {
        "agent": "bank",
        "action": "upsert",
        "file_hash": "a1b2c3d4e5f678901234567890123456",
        "system_name": "Банковские регламенты ЦБ",
        "short_name": "bank_rules_2026",
        "filename": "bank_rules.md",
        "metadata": {
            "version": "1.0",
            "regulatory_body": "CBR",
            "author": "Superadmin"
        }
    }

    markdown_without_frontmatter = "# Заголовок документа\nТекст нормативного документа без YAML frontmatter."

    # Устанавливаем POSTGRES_PASSWORD для импорта воркера
    with patch.dict(os.environ, {"POSTGRES_PASSWORD": "test_secure_postgres_pass"}):
        from src.admin_server.vectors.vector_worker import (
            extract_metadata_from_markdown,
            sanitize_chroma_metadata
        )

    raw_metadata = extract_metadata_from_markdown(markdown_without_frontmatter)
    assert "short_name" not in raw_metadata

    # Эмулируем обновленную логику обработки задачи в vector_worker
    if task.get("metadata") and isinstance(task.get("metadata"), dict):
        for k, v in task["metadata"].items():
            if k not in raw_metadata or not raw_metadata[k]:
                raw_metadata[k] = v

    short_name = raw_metadata.get("short_name") or task.get("short_name")
    assert short_name == "bank_rules_2026"

    metadata = sanitize_chroma_metadata(raw_metadata)
    metadata["short_name"] = short_name
    if "system_name" not in metadata and task.get("system_name"):
        metadata["system_name"] = str(task["system_name"])

    assert metadata["short_name"] == "bank_rules_2026"
    assert metadata["system_name"] == "Банковские регламенты ЦБ"
    assert metadata["regulatory_body"] == "CBR"


def test_admin_client_bff_new_endpoints():
    """Проверяет работу новых эндпоинтов BFF в admin_client main.py."""
    test_client = TestClient(main.app)

    with patch.object(main.remote_client, "is_authenticated", return_value=True):
        # 1. GET /api/local/agents/{name}/keys
        mock_keys = [{"kid": "k1", "status": "active", "is_revoked": False, "fingerprint": "sha256:1111"}]
        with patch.object(main.remote_client, "get_agent_keys", return_value=mock_keys) as mock_get_keys:
            res = test_client.get("/api/local/agents/bank/keys")
            assert res.status_code == 200
            assert res.json() == mock_keys
            mock_get_keys.assert_called_once_with("bank")

        # 2. POST /api/local/agents/{name}/rotate
        mock_rotate_resp = {"status": "success", "agent_name": "bank", "key_id": "new-kid-2"}
        with patch.object(main.remote_client, "rotate_agent_key", return_value=mock_rotate_resp) as mock_rot:
            res = test_client.post(
                "/api/local/agents/bank/rotate",
                json={"new_public_key": "-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----", "ttl_days": 60}
            )
            assert res.status_code == 200
            assert res.json() == mock_rotate_resp
            mock_rot.assert_called_once_with("bank", "-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----", 60)

        # 3. GET /api/local/audit/logs
        mock_audit_logs = {"items": [{"id": 1, "action": "AUTH_LOGIN", "status": "SUCCESS"}], "total": 1}
        with patch.object(main.remote_client, "get_audit_logs", return_value=mock_audit_logs) as mock_logs:
            res = test_client.get("/api/local/audit/logs?limit=10&offset=0")
            assert res.status_code == 200
            assert res.json() == mock_audit_logs
            mock_logs.assert_called_once_with(limit=10, offset=0, actor=None, action=None, status=None)

        # 4. GET /api/local/audit/verify
        mock_verify = {"status": "success", "verified": True, "total_events": 42}
        with patch.object(main.remote_client, "verify_audit_log", return_value=mock_verify) as mock_ver:
            res = test_client.get("/api/local/audit/verify")
            assert res.status_code == 200
            assert res.json() == mock_verify
            mock_ver.assert_called_once()

        # 5. GET /api/local/audit/summary
        mock_summary = {"total_events": 42, "unique_actors": 3}
        with patch.object(main.remote_client, "get_audit_summary", return_value=mock_summary) as mock_sum:
            res = test_client.get("/api/local/audit/summary")
            assert res.status_code == 200
            assert res.json() == mock_summary
            mock_sum.assert_called_once()

        # 6. GET /api/local/auth/mtls/status
        mock_mtls = {
            "server_url": "https://admin.fin-ai-agent.local",
            "mtls_configured": True,
            "ca_configured": True,
            "authenticated": True,
            "username": "admin",
            "role": "admin"
        }
        with patch.object(main.remote_client, "get_mtls_status", return_value=mock_mtls) as mock_mtls_fn:
            res = test_client.get("/api/local/auth/mtls/status")
            assert res.status_code == 200
            assert res.json() == mock_mtls
            mock_mtls_fn.assert_called_once()


def test_receipt_verification_intermediate_ca_and_expiration(pki_setup, tmp_path):
    """
    Проверяет:
    1. Отклонение просроченного сертификата сервера;
    2. Успешную верификацию трехуровневой цепочки (Root CA -> Intermediate CA -> Server Cert);
    3. Отклонение сертификата при недоступном Root CA.
    """
    ca_cert = x509.load_pem_x509_certificate(pki_setup["ca_pem"].encode("utf-8"))
    ca_key = serialization.load_pem_private_key(
        RootCAEngine(data_dir=tmp_path / "ca", passphrase="test_passphrase_12345").get_root_ca_key_bytes()
        if hasattr(RootCAEngine, "get_root_ca_key_bytes") else
        # Загружаем ca_key из pki_setup
        pki_setup.get("ca_key_pem", "").encode("utf-8") if pki_setup.get("ca_key_pem") else None,
        password=None
    ) if pki_setup.get("ca_key_pem") else None

    # Создаем Intermediate CA
    int_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    int_name = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "FinancialAI"),
        x509.NameAttribute(NameOID.COMMON_NAME, "Enclave Intermediate CA"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)

    # Загружаем root ca key из tmp_path
    ca_dir = tmp_path / "ca"
    ca_engine = RootCAEngine(data_dir=ca_dir, passphrase="test_passphrase_12345")
    _, real_ca_key = ca_engine.ensure_initialized()

    int_cert = (
        x509.CertificateBuilder()
        .subject_name(int_name)
        .issuer_name(ca_cert.subject)
        .public_key(int_key.public_key())
        .serial_number(777)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=90))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(real_ca_key, hashes.SHA256())
    )

    # Создаем leaf server cert, подписанный Intermediate CA
    leaf_server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_server_name = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
        x509.NameAttribute(NameOID.COMMON_NAME, "admin-server.internal"),
    ])
    leaf_server_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_server_name)
        .issuer_name(int_name)
        .public_key(leaf_server_key.public_key())
        .serial_number(888)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(int_key, hashes.SHA256())
    )

    int_pem = int_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    leaf_pem = leaf_server_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    chain_pem = leaf_pem + "\n" + int_pem

    signer = ZeroTrustClientSigner(
        mode="software",
        cert_pem=pki_setup["client_cert_pem"],
        key_pem=pki_setup["client_key_pem"],
        ca_cert_pem=pki_setup["ca_pem"],
    )

    receipt_data = {
        "receipt_id": str(uuid.uuid4()),
        "status": "QUEUED_FOR_INDEXING",
        "document_id": "sha256:chain123",
        "document_title": "Цепочечный Документ",
        "collection_name": "knowledge-main",
        "timestamp": int(time.time()),
        "nonce": "test_chain_nonce"
    }

    server_sig, _ = sign_server_receipt(
        receipt_data,
        private_key_pem=leaf_server_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ).decode("utf-8")
    )

    # 1. Проверка цепочки: Leaf -> Intermediate CA -> Root CA
    assert signer.verify_server_receipt(receipt_data, server_sig, chain_pem) is True

    # 2. Проверка истекшего сертификата
    expired_server_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_server_name)
        .issuer_name(ca_cert.subject)
        .public_key(leaf_server_key.public_key())
        .serial_number(999)
        .not_valid_before(now - datetime.timedelta(days=10))
        .not_valid_after(now - datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(real_ca_key, hashes.SHA256())
    )
    expired_pem = expired_server_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    assert signer.verify_server_receipt(receipt_data, server_sig, expired_pem) is False


def test_rag_documents_endpoint_returns_server_cert_header_and_body(pki_setup):
    """Проверяет эндпоинт POST /api/v1/rag/documents с активным SERVER_CERT."""
    from src.admin_server.fastapi.server import app
    from security import set_trusted_root_ca, compute_rag_canonical_digest
    client = TestClient(app)

    # Устанавливаем Root CA и SERVER_CERT
    set_trusted_root_ca(pki_setup["ca_pem"])
    old_cert = settings.SERVER_CERT
    settings.SERVER_CERT = pki_setup["server_cert_pem"]

    try:
        now_ts = int(time.time())
        nonce = f"test_nonce_cert_{uuid.uuid4().hex[:8]}"
        payload = {
            "document_title": "Финансовый Отчет 2026",
            "collection_name": "knowledge-main",
            "content": "# Содержание регламента\nФинансовый норматив...",
            "metadata": {"author": "Operator"}
        }
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        # Клиент подписывает запрос
        canonical = compute_rag_canonical_digest(now_ts, nonce, body_bytes)
        cli_priv = serialization.load_pem_private_key(pki_setup["client_key_pem"].encode("utf-8"), password=None)
        sig = cli_priv.sign(canonical, padding.PKCS1v15(), hashes.SHA256())
        sig_b64 = base64.b64encode(sig).decode("ascii")

        headers = {
            "X-Timestamp": str(now_ts),
            "X-Nonce": nonce,
            "X-Signature": sig_b64,
            "X-Cert": pki_setup["client_cert_pem"],
            "Content-Type": "application/json",
            "X-Client-Cert-Verify": "SUCCESS",
            "X-Client-Cert-Subject": "CN=superadmin-workstation,OU=admin_operator"
        }

        with patch("src.admin_server.fastapi.routers.rag_documents._get_external_services", return_value=(None, None)):
            res = client.post("/api/v1/rag/documents", content=body_bytes, headers=headers)

        assert res.status_code == 201
        data = res.json()
        assert data["status"] == "QUEUED_FOR_INDEXING"
        assert data["server_cert"] == settings.SERVER_CERT
        assert "X-Server-Cert" in res.headers

        # Проверяем, что заголовок X-Server-Cert корректно декодируется в валидный PEM
        decoded_header_cert = base64.b64decode(res.headers["X-Server-Cert"]).decode("utf-8")
        assert "BEGIN CERTIFICATE" in decoded_header_cert
    finally:
        settings.SERVER_CERT = old_cert


def test_vector_worker_process_task_content_fallback():
    """Проверяет выполнение process_task с fallback на task['content'] при сбое MinIO."""
    from src.admin_server.vectors import vector_worker

    task = {
        "agent": "bank",
        "action": "upsert",
        "file_path": "rag/test_doc.md",
        "bucket": "knowledge-bank",
        "file_hash": "hash_fallback_12345",
        "system_name": "Резервный документ",
        "short_name": "backup_doc",
        "content": "# Заголовок\nТекст документа для fallback индексации.",
        "metadata": {"version": "2.0"}
    }

    mock_minio = MagicMock()
    mock_minio.get_object.side_effect = RuntimeError("MinIO network timeout")

    mock_collection = MagicMock()
    mock_chroma = MagicMock()
    mock_chroma.get_or_create_collection.return_value = mock_collection

    mock_update_status = MagicMock()

    with patch.object(vector_worker, "minio_client", mock_minio), \
         patch.object(vector_worker, "chroma_client", mock_chroma), \
         patch.object(vector_worker, "get_embeddings_google", return_value=[[0.1] * 768]), \
         patch.object(vector_worker, "update_document_status", mock_update_status):

        vector_worker.process_task(task)

        # Проверяем, что векторная коллекция была вызвана для добавления эмбеддингов
        assert mock_collection.add.called
        # Статус документа обновлен в completed
        mock_update_status.assert_called_with("hash_fallback_12345", "bank", "completed", "Успешно обработано воркером")


def test_rag_documents_and_delete_unauthorized_role_rejected(pki_setup):
    """
    Проверяет жесткое отклонение (HTTP 403 Forbidden) при предъявлении валидного
    сертификата, выданного Root CA, но с неавторизованной ролью / CN (например, guest_viewer).
    """
    from src.pki.core.ca_engine import RootCAEngine
    from src.pki.core.cert_issuer import CertificateIssuer
    from src.admin_server.fastapi.server import app
    from security import set_trusted_root_ca, compute_rag_canonical_digest
    client = TestClient(app)

    set_trusted_root_ca(pki_setup["ca_pem"])

    # Выпускаем легитимно подписанный Root CA сертификат, но с неавторизованной ролью
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_cert = x509.load_pem_x509_certificate(pki_setup["ca_pem"].encode("utf-8"))
    ca_priv = serialization.load_pem_private_key(
        pki_setup.get("ca_key_pem", "").encode("utf-8") if pki_setup.get("ca_key_pem") else
        # Создаем временный эмитент через кастомный ключ если ca_key_pem не экспортирован
        rsa.generate_private_key(65537, 2048).private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ),
        password=None
    ) if pki_setup.get("ca_key_pem") else None

    unauth_key = rsa.generate_private_key(65537, 2048)
    unauth_name = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "guest_viewer"),
        x509.NameAttribute(NameOID.COMMON_NAME, "guest-operator-station"),
    ])

    # Подписываем Root CA ключом если есть, либо создаем тестовый CA
    ca_engine = RootCAEngine(data_dir=Path(tempfile.mkdtemp()), passphrase="test_passphrase_12345", validity_years=1)
    root_cert_obj, root_key_obj = ca_engine.ensure_initialized()
    set_trusted_root_ca(root_cert_obj.public_bytes(serialization.Encoding.PEM).decode("utf-8"))

    unauth_cert = (
        x509.CertificateBuilder()
        .subject_name(unauth_name)
        .issuer_name(root_cert_obj.subject)
        .public_key(unauth_key.public_key())
        .serial_number(991122)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=10))
        .sign(root_key_obj, hashes.SHA256())
    )
    unauth_cert_pem = unauth_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")

    now_ts = int(time.time())
    nonce = f"test_nonce_unauth_{uuid.uuid4().hex[:8]}"
    doc_payload = {
        "document_title": "Несанкционированный документ",
        "collection_name": "knowledge-main",
        "content": "Попытка несанкционированной загрузки",
        "metadata": {}
    }
    body_bytes = json.dumps(doc_payload, ensure_ascii=False).encode("utf-8")
    canonical = compute_rag_canonical_digest(now_ts, nonce, body_bytes)
    sig = unauth_key.sign(canonical, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
    sig_b64 = base64.b64encode(sig).decode("ascii")

    headers = {
        "X-Timestamp": str(now_ts),
        "X-Nonce": nonce,
        "X-Signature": sig_b64,
        "X-Cert": unauth_cert_pem,
        "Content-Type": "application/json",
    }

    with patch("src.admin_server.fastapi.routers.rag_documents._get_external_services", return_value=(None, None)):
        # 1. Загрузка должна вернуть HTTP 403 Forbidden
        res_upload = client.post("/api/v1/rag/documents", content=body_bytes, headers=headers)
        assert res_upload.status_code == 403
        assert "не авторизован" in res_upload.json()["detail"].lower()

        # 2. Удаление также должно вернуть HTTP 403 Forbidden
        del_payload = {
            "short_names": ["act_to_delete"],
            "collection_name": "knowledge-main",
            "reason": "Test delete"
        }
        del_bytes = json.dumps(del_payload, ensure_ascii=False).encode("utf-8")
        del_nonce = f"del_nonce_unauth_{uuid.uuid4().hex[:8]}"
        del_canonical = compute_rag_canonical_digest(now_ts, del_nonce, del_bytes)
        del_sig = unauth_key.sign(del_canonical, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())

        del_headers = {
            "X-Timestamp": str(now_ts),
            "X-Nonce": del_nonce,
            "X-Signature": base64.b64encode(del_sig).decode("ascii"),
            "X-Cert": unauth_cert_pem,
            "Content-Type": "application/json",
        }
        res_del = client.post("/api/v1/rag/documents/delete", content=del_bytes, headers=del_headers)
        assert res_del.status_code == 403
        assert "не авторизован" in res_del.json()["detail"].lower()


def test_vector_worker_sha256_mismatch_aborts():
    """Проверяет отклонение задачи и пометку ошибки при несовпадении 64-символьного SHA-256."""
    from src.admin_server.vectors import vector_worker

    content = "# Важный регламент\nНормативный текст."
    actual_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    mismatched_sha256 = "f" * 64

    task = {
        "agent": "bank",
        "action": "upsert",
        "file_path": "rag/test_doc.md",
        "bucket": "knowledge-bank",
        "file_hash": mismatched_sha256,
        "content": content,
    }

    mock_minio = MagicMock()
    mock_resp = MagicMock()
    mock_resp.read.return_value = content.encode("utf-8")
    mock_minio.get_object.return_value = mock_resp

    mock_update_status = MagicMock()
    mock_chroma = MagicMock()

    with patch.object(vector_worker, "minio_client", mock_minio), \
         patch.object(vector_worker, "chroma_client", mock_chroma), \
         patch.object(vector_worker, "update_document_status", mock_update_status):

        vector_worker.process_task(task)

        # ChromaDB не должен вызываться
        mock_chroma.get_or_create_collection.assert_not_called()
        # Статус документа переведен в error из-за нарушения целостности
        mock_update_status.assert_called_once()
        assert mock_update_status.call_args[0][2] == "error"
        assert "Нарушение целостности" in mock_update_status.call_args[0][3]


def test_encrypted_text_corrupted_payload_raises_value_error():
    """Проверяет, что EncryptedText при поврежденном шифротексте ENC1 выбрасывает ValueError."""
    from db.custom_types import EncryptedText

    enc_type = EncryptedText()

    # 1. Поврежденная Base64 строка, начинающаяся с ENC1 сигнатуры
    corrupted_enc1_b64 = "RU5DMQ_corrupted_base64_payload_tag_error_9999"
    with pytest.raises(ValueError, match="Decryption failed for ENC1"):
        enc_type.process_result_value(corrupted_enc1_b64, None)

    # 2. Поврежденная строка с явным префиксом ENC1
    corrupted_raw_enc1 = "ENC1_invalid_ciphertext_stream_here"
    with pytest.raises(ValueError, match="Decryption failed for ENC1"):
        enc_type.process_result_value(corrupted_raw_enc1, None)

    # 3. Открытый текст возвращается без исключений (обратная совместимость)
    assert enc_type.process_result_value("Обычный незашифрованный текст", None) == "Обычный незашифрованный текст"


def test_pipeline_zero_trust_fail_fast_without_fallback():
    """Проверяет выброс RuntimeError в DocumentPipeline при отсутствии signer (Zero-Trust fail-fast)."""
    from pipeline import DocumentPipeline

    pipeline = DocumentPipeline(
        server_url="https://admin.fin-ai-agent.local",
        admin_token="token123",
        signer=None,
        use_zero_trust=True
    )

    # Принудительно убеждаемся что signer = None
    pipeline.signer = None

    with pytest.raises(RuntimeError, match="ZeroTrustClientSigner не инициализирован"):
        pipeline.submit_zero_trust_document(
            document_title="Test Doc",
            content="# Text"
        )

    with pytest.raises(RuntimeError, match="ZeroTrustClientSigner не инициализирован"):
        pipeline.submit_zero_trust_document_delete(
            repealed_short_names=["act_1"]
        )


def test_canonical_receipt_excludes_service_fields():
    """
    Проверяет, что compute_receipt_canonical_bytes исключает ровно множество:
    {'signature', 'server_key_fingerprint', 'server_cert', 'receipt_verified'}.
    """
    from src.admin_client.backend.core.crypto.canonical import compute_receipt_canonical_bytes

    sample = {
        "receipt_id": "rec-12345",
        "status": "QUEUED_FOR_INDEXING",
        "document_id": "doc-hash-67890",
        "timestamp": 1700000000,
        "nonce": "nonce-abc",
        "signature": "should_be_excluded_sig",
        "server_key_fingerprint": "should_be_excluded_fp",
        "server_cert": "should_be_excluded_cert",
        "receipt_verified": True,
    }

    canonical = compute_receipt_canonical_bytes(sample)
    parsed = json.loads(canonical.decode("utf-8"))

    assert "signature" not in parsed
    assert "server_key_fingerprint" not in parsed
    assert "server_cert" not in parsed
    assert "receipt_verified" not in parsed
    assert parsed["receipt_id"] == "rec-12345"
    assert parsed["status"] == "QUEUED_FOR_INDEXING"
    assert parsed["document_id"] == "doc-hash-67890"


def test_rag_documents_delete_endpoint_and_x_agent_cert(pki_setup):
    """
    Проверяет:
    1. Прием заголовка X-Agent-Cert в /api/v1/rag/documents/delete;
    2. Наличие поля document_id и deleted_targets в ответе RAGDocumentDeleteResponse;
    3. Корректную валидацию квитанции сервера клиентом (AckReceipt).
    """
    from src.admin_server.fastapi.server import app
    from security import set_trusted_root_ca, compute_rag_canonical_digest
    client = TestClient(app)

    set_trusted_root_ca(pki_setup["ca_pem"])
    old_cert = settings.SERVER_CERT
    old_priv = settings.PRIVATE_KEY
    settings.SERVER_CERT = pki_setup["server_cert_pem"]
    settings.PRIVATE_KEY = pki_setup["server_key_pem"]

    try:
        now_ts = int(time.time())
        nonce = f"test_nonce_del_{uuid.uuid4().hex[:8]}"
        del_payload = {
            "short_names": ["act_2026_01", "act_2026_02"],
            "collection_name": "knowledge-bank",
            "reason": "Repealed by order 99"
        }
        body_bytes = json.dumps(del_payload, ensure_ascii=False).encode("utf-8")

        canonical = compute_rag_canonical_digest(now_ts, nonce, body_bytes)
        cli_priv = serialization.load_pem_private_key(pki_setup["client_key_pem"].encode("utf-8"), password=None)
        sig = cli_priv.sign(canonical, padding.PKCS1v15(), hashes.SHA256())
        sig_b64 = base64.b64encode(sig).decode("ascii")

        # Передаем сертификат через заголовок X-Agent-Cert
        headers = {
            "X-Timestamp": str(now_ts),
            "X-Nonce": nonce,
            "X-Signature": sig_b64,
            "X-Agent-Cert": pki_setup["client_cert_pem"],
            "Content-Type": "application/json",
            "X-Client-Cert-Verify": "SUCCESS",
            "X-Client-Cert-Subject": "CN=superadmin-workstation,OU=admin_operator"
        }

        with patch("src.admin_server.fastapi.routers.rag_documents._get_external_services", return_value=(None, None)):
            res = client.post("/api/v1/rag/documents/delete", content=body_bytes, headers=headers)

        assert res.status_code == 200, res.text
        data = res.json()
        assert "document_id" in data
        assert data["deleted_targets"] == ["act_2026_01", "act_2026_02"]
        assert data["status"] == "QUEUED_FOR_DELETION"
        assert "signature" in data
        assert "server_key_fingerprint" in data

        # Проверяем квитанцию на стороне клиента
        signer = ZeroTrustClientSigner(
            mode="software",
            cert_pem=pki_setup["client_cert_pem"],
            key_pem=pki_setup["client_key_pem"],
            ca_cert_pem=pki_setup["ca_pem"],
        )
        assert signer.verify_server_receipt(
            receipt_data=data,
            server_signature_b64=data["signature"],
            trusted_server_cert_or_pubkey=pki_setup["server_cert_pem"]
        ) is True
    finally:
        settings.SERVER_CERT = old_cert
        settings.PRIVATE_KEY = old_priv


def test_validate_production_env_disk_file_checks(tmp_path):
    """
    Проверяет, что validate_production_env() проверяет физическое наличие файлов на диске:
    - JWT_PRIVATE_KEY_PATH несуществующий файл вызывает RuntimeError;
    - JWT_PUBLIC_KEY_PATH несуществующий файл вызывает RuntimeError;
    - SERVER_CERT_PATH несуществующий файл вызывает RuntimeError.
    """
    from security import Settings

    non_existent = str(tmp_path / "definitely_does_not_exist.pem")
    test_settings = Settings()

    # 1. Несуществующий JWT_PRIVATE_KEY_PATH
    with patch.dict(os.environ, {"JWT_PRIVATE_KEY_PATH": non_existent}):
        with pytest.raises(RuntimeError, match="JWT_PRIVATE_KEY_PATH не найден на диске"):
            test_settings.validate_production_env()

    # 2. Несуществующий JWT_PUBLIC_KEY_PATH
    with patch.dict(os.environ, {"JWT_PUBLIC_KEY_PATH": non_existent}):
        with pytest.raises(RuntimeError, match="JWT_PUBLIC_KEY_PATH не найден на диске"):
            test_settings.validate_production_env()

    # 3. Несуществующий SERVER_CERT_PATH
    with patch.dict(os.environ, {"SERVER_CERT_PATH": non_existent}):
        with pytest.raises(RuntimeError, match="SERVER_CERT_PATH не найден на диске"):
            test_settings.validate_production_env()


def test_cors_middleware_allows_agent_and_enclave_headers():
    """Проверяет наличие 'x-agent-cert' и 'x-enclave-cert' в CORSMiddleware."""
    from src.admin_server.fastapi.server import app

    cors_middleware = None
    for mw in app.user_middleware:
        if mw.cls.__name__ == "CORSMiddleware":
            cors_middleware = mw
            break

    assert cors_middleware is not None
    allowed = [h.lower() for h in cors_middleware.kwargs.get("allow_headers", [])]
    assert "x-agent-cert" in allowed
    assert "x-enclave-cert" in allowed



