"""
Модуль тестирования Фазы 5: Клиент Администратора (Zero-Trust mTLS, аппаратная/биометрическая
подпись TPM 2.0 / Windows Hello, верификация серверных квитанций AckReceipt и интеграция пайплайна).
"""

import os
import sys
import json
import time
import uuid
import base64
from unittest.mock import MagicMock, patch
import pytest

from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes, serialization

# Настройка путей для импорта клиента и сервера
backend_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "admin_client", "backend"))
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)

admin_server_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "admin_server", "fastapi"))
if admin_server_path not in sys.path:
    sys.path.insert(0, admin_server_path)

try:
    from src.admin_client.backend.core.crypto.canonical import compute_rag_canonical_digest, compute_receipt_canonical_bytes
    from src.admin_client.backend.core.crypto.hardware_bridge import HardwareSigningBridge, SimulatedTPMBridge
    from src.admin_client.backend.core.crypto.software_bridge import SoftwareSigningBridge
    from src.admin_client.backend.core.crypto.signer import ZeroTrustClientSigner
    from src.admin_client.backend.pipeline import DocumentPipeline
    from src.admin_client.backend import main
except ImportError:
    from core.crypto.canonical import compute_rag_canonical_digest, compute_receipt_canonical_bytes
    from core.crypto.hardware_bridge import HardwareSigningBridge, SimulatedTPMBridge
    from core.crypto.software_bridge import SoftwareSigningBridge
    from core.crypto.signer import ZeroTrustClientSigner
    from pipeline import DocumentPipeline
    import main


# --- Фикстуры ---

@pytest.fixture
def sample_server_keypair():
    """Генерирует пару ключей сервера для эмуляции подписи квитанций."""
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


# --- ТЕСТЫ ---

def test_hardware_signing_bridge_tpm_detection():
    """Проверяет метод обнаружения TPM 2.0 без выброса исключений."""
    is_tpm = HardwareSigningBridge.is_tpm_available()
    assert isinstance(is_tpm, bool)
    bridge = HardwareSigningBridge()
    assert isinstance(bridge.is_available(), bool)


def test_simulated_tpm_bridge_biometrics_success_and_denial():
    """Проверяет работу эмулятора TPM 2.0 и поведение при биометрическом подтверждении/отказе."""
    # 1. Успешная биометрическая авторизация (палец приложен)
    tpm_ok = SimulatedTPMBridge(biometric_prompt_callback=lambda: True)
    assert tpm_ok.get_certificate_pem().startswith("-----BEGIN CERTIFICATE-----")
    assert tpm_ok.get_public_key_pem().startswith("-----BEGIN PUBLIC KEY-----")
    
    test_data = b"Canonical payload to sign"
    sig_pss = tpm_ok.sign_digest(test_data, scheme="PSS")
    assert isinstance(sig_pss, str) and len(sig_pss) > 50

    sig_pkcs1 = tpm_ok.sign_digest(test_data, scheme="PKCS1")
    assert isinstance(sig_pkcs1, str) and len(sig_pkcs1) > 50

    # 2. Отказ биометрии (пользователь отклонил окно Windows Hello)
    tpm_denied = SimulatedTPMBridge(biometric_prompt_callback=lambda: False)
    with pytest.raises(PermissionError) as exc_info:
        tpm_denied.sign_digest(test_data)
    assert "Windows Hello отклонена" in str(exc_info.value)


def test_software_signing_bridge_key_generation_and_signing():
    """Проверяет работу программного моста с генерацией ключей и подписью."""
    sw_bridge = SoftwareSigningBridge()
    assert sw_bridge.get_certificate_pem().startswith("-----BEGIN CERTIFICATE-----")
    assert sw_bridge.get_public_key_pem().startswith("-----BEGIN PUBLIC KEY-----")
    
    test_payload = b"Sample financial statement payload"
    sig = sw_bridge.sign_digest(test_payload, scheme="PSS")
    assert isinstance(sig, str) and len(sig) > 64


def test_zero_trust_client_signer_initialization_modes():
    """Проверяет инициализацию ZeroTrustClientSigner в разных режимах и обработку ошибок."""
    # Режим simulated_tpm
    signer_sim = ZeroTrustClientSigner(mode="simulated_tpm")
    assert isinstance(signer_sim._active_bridge, SimulatedTPMBridge)
    assert signer_sim.get_key_fingerprint().startswith("sha256:")

    # Режим software
    signer_sw = ZeroTrustClientSigner(mode="software")
    assert isinstance(signer_sw._active_bridge, SoftwareSigningBridge)

    # Режим auto
    signer_auto = ZeroTrustClientSigner(mode="auto")
    assert signer_auto.get_certificate_pem()

    # Режим hardware: при отсутствии TPM на платформе вызывает ошибку
    with patch.object(HardwareSigningBridge, "is_tpm_available", return_value=False):
        with pytest.raises(RuntimeError) as exc_info:
            ZeroTrustClientSigner(mode="hardware")
        assert "TPM 2.0 недоступен" in str(exc_info.value)


def test_zero_trust_client_signer_headers_generation_and_replay_protection():
    """Проверяет генерацию заголовков запроса, уникальность Nonce и актуальность Timestamp."""
    signer = ZeroTrustClientSigner(mode="simulated_tpm")
    raw_body = json.dumps({"document_title": "Quarterly Report", "content": "Sample content"}).encode("utf-8")

    headers1 = signer.create_rag_request_headers(raw_body)
    headers2 = signer.create_rag_request_headers(raw_body)

    for h in ("X-Timestamp", "X-Nonce", "X-Signature", "X-Cert"):
        assert h in headers1
        assert h in headers2

    # Защита от Replay-атак: Nonce обязан быть уникальным для каждого запроса
    assert headers1["X-Nonce"] != headers2["X-Nonce"]

    # Timestamp должен быть актуальным (в пределах 5 секунд)
    ts = int(headers1["X-Timestamp"])
    assert abs(time.time() - ts) < 5


def test_zero_trust_client_signature_interoperability_with_server():
    """Проверяет, что подпись, сгенерированная клиентом, успешно валидируется функцией verify_digital_signature сервера."""
    from security import verify_digital_signature, compute_rag_canonical_digest as server_canonical

    signer = ZeroTrustClientSigner(mode="simulated_tpm")
    test_body = b'{"document_title":"Audit Report 2026","content":"Classified"}'
    
    headers = signer.create_rag_request_headers(test_body)
    
    # Сервер восстанавливает канонический дайджест
    server_digest = server_canonical(
        timestamp=int(headers["X-Timestamp"]),
        nonce=headers["X-Nonce"],
        body_bytes=test_body
    )
    
    # Сервер проверяет цифровую подпись публичным сертификатом клиента
    is_valid = verify_digital_signature(
        public_key_or_cert=headers["X-Cert"],
        signature_b64=headers["X-Signature"],
        data_bytes=server_digest
    )
    assert is_valid is True

    # Проверка обнаружения подделки полезной нагрузки
    tampered_body = b'{"document_title":"Audit Report 2026","content":"TAMPERED"}'
    tampered_digest = server_canonical(
        timestamp=int(headers["X-Timestamp"]),
        nonce=headers["X-Nonce"],
        body_bytes=tampered_body
    )
    assert verify_digital_signature(
        public_key_or_cert=headers["X-Cert"],
        signature_b64=headers["X-Signature"],
        data_bytes=tampered_digest
    ) is False


def test_zero_trust_client_server_receipt_verification_and_tampering(sample_server_keypair):
    """Проверяет верификацию серверной квитанции AckReceipt и обнаружение ее модификации."""
    server_priv, _, server_pub_pem = sample_server_keypair
    signer = ZeroTrustClientSigner(mode="simulated_tpm")

    receipt_data = {
        "receipt_id": str(uuid.uuid4()),
        "status": "QUEUED_FOR_INDEXING",
        "document_id": "sha256-hash-12345",
        "document_title": "Annual Audit",
        "collection_name": "knowledge-main",
        "timestamp": int(time.time()),
        "nonce": "test-nonce-777"
    }

    # Сервер подписывает каноническую квитанцию
    canonical_receipt = compute_receipt_canonical_bytes(receipt_data)
    server_sig_bytes = server_priv.sign(
        canonical_receipt,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256()
    )
    server_sig_b64 = base64.b64encode(server_sig_bytes).decode("ascii")

    # 1. Корректная квитанция успешно проходит проверку клиентом
    is_valid = signer.verify_server_receipt(
        receipt_data=receipt_data,
        server_signature_b64=server_sig_b64,
        trusted_server_cert_or_pubkey=server_pub_pem
    )
    assert is_valid is True

    # 2. Модифицированная квитанция (подмена статуса) отклоняется
    tampered_receipt = dict(receipt_data)
    tampered_receipt["status"] = "FORGED_STATUS"
    assert signer.verify_server_receipt(
        receipt_data=tampered_receipt,
        server_signature_b64=server_sig_b64,
        trusted_server_cert_or_pubkey=server_pub_pem
    ) is False

    # 3. Поддельная подпись отклоняется
    assert signer.verify_server_receipt(
        receipt_data=receipt_data,
        server_signature_b64="bad-signature-base64",
        trusted_server_cert_or_pubkey=server_pub_pem
    ) is False


def test_document_pipeline_mtls_session_setup(tmp_path):
    """Проверяет настройку параметров mTLS (verify и cert) в сессии DocumentPipeline."""
    ca_file = tmp_path / "ca.crt"
    cert_file = tmp_path / "admin_client.crt"
    key_file = tmp_path / "admin_client.key"
    ca_file.write_text("DUMMY CA", encoding="utf-8")
    cert_file.write_text("DUMMY CERT", encoding="utf-8")
    key_file.write_text("DUMMY KEY", encoding="utf-8")

    pipeline = DocumentPipeline(
        server_url="https://admin.financial.local",
        admin_token="header.payload.signature",
        ca_cert_path=str(ca_file),
        client_cert_path=str(cert_file),
        client_key_path=str(key_file)
    )

    assert pipeline.session.verify == str(ca_file)
    assert pipeline.session.cert == (str(cert_file), str(key_file))
    assert pipeline.use_zero_trust is True
    assert pipeline.signer is not None


def test_document_pipeline_zero_trust_rag_submission_and_fallback(sample_server_keypair):
    """Проверяет вызов submit_zero_trust_document и корректную обработку ответа с валидацией квитанции."""
    server_priv, _, server_pub_pem = sample_server_keypair

    signer = ZeroTrustClientSigner(mode="simulated_tpm")
    pipeline = DocumentPipeline(
        server_url="https://admin.financial.local",
        admin_token="header.payload.signature",
        signer=signer,
        use_zero_trust=True
    )

    fake_receipt_id = str(uuid.uuid4())
    receipt_data = {
        "receipt_id": fake_receipt_id,
        "status": "QUEUED_FOR_INDEXING",
        "document_id": "sha256-doc-999",
        "document_title": "Finances",
        "collection_name": "knowledge-main",
        "timestamp": int(time.time()),
        "nonce": "nonce-12345"
    }
    canonical_receipt = compute_receipt_canonical_bytes(receipt_data)
    sig_raw = server_priv.sign(
        canonical_receipt,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256()
    )
    receipt_data["signature"] = base64.b64encode(sig_raw).decode("ascii")

    # Мокируем ответ POST /api/v1/rag/documents
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = receipt_data
    mock_resp.headers = {"X-Admin-Signature": receipt_data["signature"]}
    mock_resp.raise_for_status.return_value = None

    with patch.object(pipeline.session, "post", return_value=mock_resp) as mock_post:
        with patch.object(signer, "verify_server_receipt", return_value=True):
            res = pipeline.submit_zero_trust_document(
                document_title="Finances",
                content="# Heading\nBody content",
                collection_name="knowledge-main"
            )
            assert res["receipt_id"] == fake_receipt_id
            assert res["receipt_verified"] is True
            assert mock_post.called

            call_args, call_kwargs = mock_post.call_args
            assert call_args[0] == "https://admin.financial.local/api/v1/rag/documents"
            assert "X-Signature" in call_kwargs["headers"]
            assert "X-Nonce" in call_kwargs["headers"]
            assert "X-Timestamp" in call_kwargs["headers"]


def test_admin_client_main_security_status_endpoint():
    """Проверяет эндпоинт GET /api/local/security/status в main.py."""
    from fastapi.testclient import TestClient

    test_secret = "test-ipc-secret-777"
    with patch.object(main, "SIDECAR_IPC_SECRET", test_secret):
        client = TestClient(main.app)
        resp = client.get("/api/local/security/status", headers={"X-Local-Secret": test_secret})
        assert resp.status_code == 200
        data = resp.json()
        assert "hardware_tpm_available" in data
        assert "platform" in data
        assert "zero_trust_ready" in data
        assert "client_key_fingerprint" in data
