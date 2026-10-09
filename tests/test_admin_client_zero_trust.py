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

import datetime
from cryptography import x509
from cryptography.x509.oid import NameOID
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
    from src.admin_client.backend.core.crypto.cert_validator import (
        CertificateMissingError,
        WindowsCertificateStoreError,
        CertificateValidationError,
        KeyMissingError,
        find_root_ca_in_windows_store,
        find_client_cert_in_windows_store,
        export_windows_cert_to_cache,
        get_windows_cert_store_help_message,
        resolve_and_validate_client_certificate,
        create_test_ca_and_client_cert,
    )
    from src.admin_client.backend.pipeline import DocumentPipeline
    from src.admin_client.backend import main
except ImportError:
    from core.crypto.canonical import compute_rag_canonical_digest, compute_receipt_canonical_bytes
    from core.crypto.hardware_bridge import HardwareSigningBridge, SimulatedTPMBridge
    from core.crypto.software_bridge import SoftwareSigningBridge
    from core.crypto.signer import ZeroTrustClientSigner
    from core.crypto.cert_validator import (
        CertificateMissingError,
        WindowsCertificateStoreError,
        CertificateValidationError,
        KeyMissingError,
        find_root_ca_in_windows_store,
        find_client_cert_in_windows_store,
        export_windows_cert_to_cache,
        get_windows_cert_store_help_message,
        resolve_and_validate_client_certificate,
        create_test_ca_and_client_cert,
    )
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


@pytest.fixture
def test_pki_pair():
    """Генерирует легитимный Root CA и подписанный им клиентский сертификат с закрытым ключом."""
    ca_pem, cli_pem, cli_key = create_test_ca_and_client_cert()
    cli_key_pem = cli_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode("utf-8")
    return {
        "ca_pem": ca_pem,
        "cert_pem": cli_pem,
        "key_pem": cli_key_pem,
        "private_key": cli_key,
    }


# --- ТЕСТЫ ---

def test_hardware_signing_bridge_tpm_detection():
    """Проверяет метод обнаружения TPM 2.0 и полный цикл работы аппаратного моста."""
    is_tpm = HardwareSigningBridge.is_tpm_available()
    assert isinstance(is_tpm, bool)
    if is_tpm and sys.platform == "win32":
        bridge = HardwareSigningBridge(key_name="FinancialAI_Admin_TestTPMKey")
        try:
            assert bridge.is_available() is True
            pub_pem = bridge.get_public_key_pem()
            assert pub_pem.startswith("-----BEGIN PUBLIC KEY-----")
            assert bridge.get_key_fingerprint().startswith("sha256:")

            # Проверка аппаратного подписания RSA-PSS
            test_data = b"Canonical Zero-Trust Test Digest"
            sig_b64 = bridge.sign_digest(test_data, scheme="PSS")
            assert isinstance(sig_b64, str) and len(sig_b64) > 64

            # Верификация открытым ключом
            raw_sig = base64.b64decode(sig_b64)
            loaded_pub = serialization.load_pem_public_key(pub_pem.encode("utf-8"))
            loaded_pub.verify(
                raw_sig,
                test_data,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                hashes.SHA256()
            )
        finally:
            bridge.close()


def test_simulated_tpm_bridge_biometrics_success_and_denial(test_pki_pair):
    """Проверяет работу эмулятора TPM 2.0 и поведение при биометрическом подтверждении/отказе."""
    # 1. Успешная биометрическая авторизация (палец приложен)
    tpm_ok = SimulatedTPMBridge(
        biometric_prompt_callback=lambda: True,
        private_key=test_pki_pair["private_key"],
        cert_pem=test_pki_pair["cert_pem"],
        ca_cert_pem=test_pki_pair["ca_pem"]
    )
    assert tpm_ok.get_certificate_pem().startswith("-----BEGIN CERTIFICATE-----")
    assert tpm_ok.get_public_key_pem().startswith("-----BEGIN PUBLIC KEY-----")
    
    test_data = b"Canonical payload to sign"
    sig_pss = tpm_ok.sign_digest(test_data, scheme="PSS")
    assert isinstance(sig_pss, str) and len(sig_pss) > 50

    sig_pkcs1 = tpm_ok.sign_digest(test_data, scheme="PKCS1")
    assert isinstance(sig_pkcs1, str) and len(sig_pkcs1) > 50

    # 2. Отказ биометрии (пользователь отклонил окно Windows Hello)
    tpm_denied = SimulatedTPMBridge(
        biometric_prompt_callback=lambda: False,
        private_key=test_pki_pair["private_key"],
        cert_pem=test_pki_pair["cert_pem"],
        ca_cert_pem=test_pki_pair["ca_pem"]
    )
    with pytest.raises(PermissionError) as exc_info:
        tpm_denied.sign_digest(test_data)
    assert "Windows Hello отклонена" in str(exc_info.value)


def test_simulated_tpm_bridge_rejects_missing_or_self_signed_cert(test_pki_pair):
    """Проверяет строгий отказ SimulatedTPMBridge при отсутствии сертификата или самоподписанном сертификате."""
    # 1. Отсутствие сертификата выбрасывает CertificateMissingError
    with pytest.raises(CertificateMissingError) as exc_info:
        SimulatedTPMBridge(key_name="NoCertTPM")
    assert "не найден" in str(exc_info.value)

    # 2. Попытка передачи самоподписанного сертификата выбрасывает CertificateValidationError
    priv = rsa.generate_private_key(65537, 2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "self-signed")])
    now = datetime.datetime.now(datetime.timezone.utc)
    self_signed_cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(priv.public_key())
        .serial_number(12345)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(priv, hashes.SHA256())
    )
    self_signed_pem = self_signed_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")

    with pytest.raises(CertificateValidationError) as exc_info:
        SimulatedTPMBridge(
            private_key=priv,
            cert_pem=self_signed_pem,
            ca_cert_pem=test_pki_pair["ca_pem"]
        )
    assert "самоподписанным" in str(exc_info.value)


def test_software_signing_bridge_key_generation_and_signing(test_pki_pair):
    """Проверяет работу программного моста с легитимным сертификатом Root CA."""
    sw_bridge = SoftwareSigningBridge(
        key_pem=test_pki_pair["key_pem"],
        cert_pem=test_pki_pair["cert_pem"],
        ca_cert_pem=test_pki_pair["ca_pem"]
    )
    assert sw_bridge.get_certificate_pem().startswith("-----BEGIN CERTIFICATE-----")
    assert sw_bridge.get_public_key_pem().startswith("-----BEGIN PUBLIC KEY-----")
    
    test_payload = b"Sample financial statement payload"
    sig = sw_bridge.sign_digest(test_payload, scheme="PSS")
    assert isinstance(sig, str) and len(sig) > 64


def test_software_signing_bridge_rejects_missing_key_or_cert(test_pki_pair):
    """Проверяет строгий отказ SoftwareSigningBridge при отсутствии ключа или сертификата."""
    # 1. Отсутствие ключа -> KeyMissingError
    with pytest.raises(KeyMissingError) as exc_info:
        SoftwareSigningBridge()
    assert "не найден" in str(exc_info.value)

    # 2. Ключ есть, но нет сертификата -> CertificateMissingError
    with pytest.raises(CertificateMissingError) as exc_info:
        SoftwareSigningBridge(key_pem=test_pki_pair["key_pem"])
    assert "не найден" in str(exc_info.value)


def test_zero_trust_client_signer_initialization_modes(test_pki_pair):
    """Проверяет инициализацию ZeroTrustClientSigner в строгих режимах и строгий запрет 'auto'."""
    # Режим simulated_tpm
    signer_sim = ZeroTrustClientSigner(
        mode="simulated_tpm",
        key_pem=test_pki_pair["key_pem"],
        cert_pem=test_pki_pair["cert_pem"],
        ca_cert_pem=test_pki_pair["ca_pem"]
    )
    assert isinstance(signer_sim._active_bridge, SimulatedTPMBridge)
    assert signer_sim.get_key_fingerprint().startswith("sha256:")

    # Режим software
    signer_sw = ZeroTrustClientSigner(
        mode="software",
        key_pem=test_pki_pair["key_pem"],
        cert_pem=test_pki_pair["cert_pem"],
        ca_cert_pem=test_pki_pair["ca_pem"]
    )
    assert isinstance(signer_sw._active_bridge, SoftwareSigningBridge)

    # Режим auto: СТРОГО ЗАПРЕЩЕН в Zero-Trust архитектуре
    with pytest.raises(ValueError) as exc_info:
        ZeroTrustClientSigner(mode="auto")
    assert "Режим 'auto' запрещен" in str(exc_info.value)

    # Режим hardware по умолчанию
    if HardwareSigningBridge.is_tpm_available():
        signer_hw = ZeroTrustClientSigner(
            cert_pem=test_pki_pair["cert_pem"],
            ca_cert_pem=test_pki_pair["ca_pem"]
        )
        assert signer_hw.mode == "hardware"
        assert isinstance(signer_hw._active_bridge, HardwareSigningBridge)
    else:
        with pytest.raises(RuntimeError):
            ZeroTrustClientSigner()

    # Режим hardware: при отсутствии TPM на платформе вызывает ошибку
    with patch.object(HardwareSigningBridge, "is_tpm_available", return_value=False):
        with pytest.raises(RuntimeError) as exc_info:
            ZeroTrustClientSigner(mode="hardware")
        assert "TPM 2.0 недоступен" in str(exc_info.value)


def test_zero_trust_client_signer_headers_generation_and_replay_protection(test_pki_pair):
    """Проверяет генерацию заголовков запроса, уникальность Nonce и актуальность Timestamp."""
    signer = ZeroTrustClientSigner(
        mode="simulated_tpm",
        key_pem=test_pki_pair["key_pem"],
        cert_pem=test_pki_pair["cert_pem"],
        ca_cert_pem=test_pki_pair["ca_pem"]
    )
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


def test_zero_trust_client_signature_interoperability_with_server(test_pki_pair):
    """Проверяет, что подпись, сгенерированная клиентом, успешно валидируется функцией verify_digital_signature сервера."""
    from security import verify_digital_signature, compute_rag_canonical_digest as server_canonical

    signer = ZeroTrustClientSigner(
        mode="simulated_tpm",
        key_pem=test_pki_pair["key_pem"],
        cert_pem=test_pki_pair["cert_pem"],
        ca_cert_pem=test_pki_pair["ca_pem"]
    )
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


def test_zero_trust_client_server_receipt_verification_and_tampering(sample_server_keypair, test_pki_pair):
    """Проверяет верификацию серверной квитанции AckReceipt и обнаружение ее модификации."""
    server_priv, _, server_pub_pem = sample_server_keypair
    signer = ZeroTrustClientSigner(
        mode="simulated_tpm",
        key_pem=test_pki_pair["key_pem"],
        cert_pem=test_pki_pair["cert_pem"],
        ca_cert_pem=test_pki_pair["ca_pem"]
    )

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


def test_document_pipeline_mtls_session_setup(tmp_path, test_pki_pair):
    """Проверяет настройку параметров mTLS (verify и cert) в сессии DocumentPipeline."""
    ca_file = tmp_path / "ca.crt"
    cert_file = tmp_path / "admin_client.crt"
    key_file = tmp_path / "admin_client.key"
    ca_file.write_text(test_pki_pair["ca_pem"], encoding="utf-8")
    cert_file.write_text(test_pki_pair["cert_pem"], encoding="utf-8")
    key_file.write_text(test_pki_pair["key_pem"], encoding="utf-8")

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


def test_document_pipeline_zero_trust_rag_submission_and_fallback(sample_server_keypair, test_pki_pair):
    """Проверяет вызов submit_zero_trust_document и корректную обработку ответа с валидацией квитанции."""
    server_priv, _, server_pub_pem = sample_server_keypair

    signer = ZeroTrustClientSigner(
        mode="simulated_tpm",
        key_pem=test_pki_pair["key_pem"],
        cert_pem=test_pki_pair["cert_pem"],
        ca_cert_pem=test_pki_pair["ca_pem"]
    )
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
        with TestClient(main.app) as client:
            resp = client.get("/api/local/security/status", headers={"X-Local-Secret": test_secret})
            assert resp.status_code == 200
            data = resp.json()
            assert "hardware_tpm_available" in data
            assert "platform" in data
            assert "zero_trust_ready" in data
            assert "client_key_fingerprint" in data
            assert "windows_cert_store" in data
            assert "status" in data["windows_cert_store"]


def test_windows_certificate_store_error_and_help_message():
    """Проверяет генерацию понятных локализованных инструкций certmgr.msc при отсутствии сертификатов."""
    # 1. Проверка наследования исключения
    assert issubclass(WindowsCertificateStoreError, CertificateMissingError)

    # 2. Инструкция для Root CA
    msg_ca = get_windows_cert_store_help_message("root_ca")
    assert "certmgr.msc" in msg_ca
    assert "Доверенные корневые центры сертификации" in msg_ca
    assert "ca.crt" in msg_ca

    # 3. Инструкция для Client Cert
    msg_client = get_windows_cert_store_help_message("client_cert")
    assert "certmgr.msc" in msg_client
    assert "Личные" in msg_client
    assert "admin_client.crt" in msg_client

    # 4. Общая инструкция (missing both)
    msg_all = get_windows_cert_store_help_message("all")
    assert "ca.crt" in msg_all
    assert "admin_client.crt" in msg_all


def test_windows_certificate_store_export_to_cache(test_pki_pair):
    """Проверяет корректный экспорт сертификата из хранилища Windows в защищенный локальный кэш."""
    cached_file = export_windows_cert_to_cache(test_pki_pair["cert_pem"], "test_exported_cert.crt")
    assert os.path.exists(cached_file)
    try:
        with open(cached_file, "r", encoding="utf-8") as f:
            content = f.read()
        assert content.strip() == test_pki_pair["cert_pem"].strip()
    finally:
        if os.path.exists(cached_file):
            os.remove(cached_file)


def test_resolve_and_validate_client_certificate_from_windows_store_mock(test_pki_pair):
    """Проверяет прозрачное разрешение сертификатов из Windows Store при отсутствии файлов на диске."""
    cli_cert_obj = x509.load_pem_x509_certificate(test_pki_pair["cert_pem"].encode("utf-8"))
    ca_cert_obj = x509.load_pem_x509_certificate(test_pki_pair["ca_pem"].encode("utf-8"))

    with patch("src.admin_client.backend.core.crypto.cert_validator.find_certificate_file", return_value=None):
        with patch("src.admin_client.backend.core.crypto.cert_validator.find_client_cert_in_windows_store", return_value=(test_pki_pair["cert_pem"], cli_cert_obj)):
            with patch("src.admin_client.backend.core.crypto.cert_validator.find_root_ca_in_windows_store", return_value=(test_pki_pair["ca_pem"], ca_cert_obj)):
                pem, cert = resolve_and_validate_client_certificate(
                    cert_path=None,
                    ca_cert_path=None,
                    expected_public_key=test_pki_pair["private_key"].public_key()
                )
                assert pem.strip() == test_pki_pair["cert_pem"].strip()
                assert cert.subject == cli_cert_obj.subject


def test_resolve_and_validate_client_certificate_raises_when_missing_in_windows_store():
    """Проверяет выброс понятного WindowsCertificateStoreError, если сертификат отсутствует в certmgr.msc и на диске."""
    with patch("src.admin_client.backend.core.crypto.cert_validator.find_certificate_file", return_value=None):
        with patch("src.admin_client.backend.core.crypto.cert_validator.find_client_cert_in_windows_store", return_value=None):
            with pytest.raises(WindowsCertificateStoreError) as exc_info:
                resolve_and_validate_client_certificate(cert_path=None)
            err_msg = str(exc_info.value)
            assert "certmgr.msc" in err_msg
            assert "Личные" in err_msg
            assert "admin_client.crt" in err_msg


def test_resolve_and_validate_client_certificate_raises_when_root_ca_missing_in_windows_store(test_pki_pair):
    """Проверяет выброс WindowsCertificateStoreError, если Root CA отсутствует в certmgr.msc и на диске."""
    with patch("src.admin_client.backend.core.crypto.cert_validator.find_certificate_file", return_value=None):
        with patch("src.admin_client.backend.core.crypto.cert_validator.find_root_ca_in_windows_store", return_value=None):
            with pytest.raises(WindowsCertificateStoreError) as exc_info:
                resolve_and_validate_client_certificate(
                    cert_pem=test_pki_pair["cert_pem"],
                    ca_cert_path=None,
                    ca_cert_pem=None,
                    require_ca=True
                )
            err_msg = str(exc_info.value)
            assert "certmgr.msc" in err_msg
            assert "Доверенные корневые центры сертификации" in err_msg
            assert "ca.crt" in err_msg


def test_process_document_blocks_when_zero_trust_certs_missing():
    """Проверяет отказ HTTP 422 эндпоинта /api/local/process при отсутствии сертификатов в certmgr.msc."""
    from fastapi.testclient import TestClient
    import io

    test_secret = "test-ipc-secret-777"
    with patch.object(main, "SIDECAR_IPC_SECRET", test_secret):
        with patch.object(main, "find_root_ca_in_windows_store", return_value=None):
            with patch.object(main, "find_client_cert_in_windows_store", return_value=None):
                with patch("os.path.exists", return_value=False):
                    with TestClient(main.app) as client:
                        resp = client.post(
                            "/api/local/process",
                            headers={"X-Local-Secret": test_secret},
                            data={
                                "agent_name": "digital",
                                "server_url": "http://127.0.0.1:8000",
                                "admin_token": "header.payload.signature",
                                "use_zero_trust": "true",
                            },
                            files={"file": ("test.txt", io.BytesIO(b"Financial statement data"), "text/plain")}
                        )
                        assert resp.status_code == 422
                        data = resp.json()
                        assert data["detail"]["error"] == "WINDOWS_CERT_STORE_CERTIFICATE_MISSING"
                        assert "certmgr.msc" in data["detail"]["message"]

