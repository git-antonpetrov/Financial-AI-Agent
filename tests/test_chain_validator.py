"""
Тесты для Фазы 4: Валидация цепочек сертификатов X.509 (X509ChainValidator),
интеграция Банка с Root CA и подписание чеков.
"""

import os
import sys
import time
import base64
import uuid
import datetime
from pathlib import Path
import pytest

from cryptography import x509
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.backends import default_backend

from src.simulations.core.crypto.bank_ca import (
    BankCertificateAuthority,
    get_bank_ca,
    reset_bank_ca_for_tests,
)
from src.simulations.core.crypto.chain_validator import (
    X509ChainValidator,
    get_chain_validator,
    reset_chain_validator_for_tests,
)
from src.simulations.core.crypto.receipt_signer import ReceiptSigner


@pytest.fixture
def ca_and_leaf():
    """Создает тестовый Root CA и выпущенный им сертификат службы Банка."""
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Financial AI Zero-Trust PKI"),
        x509.NameAttribute(NameOID.COMMON_NAME, "Financial AI Internal Root CA"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_name = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Financial AI Zero-Trust PKI"),
        x509.NameAttribute(NameOID.COMMON_NAME, "bank-simulation-service"),
    ])
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(1001)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    return ca_cert, ca_key, leaf_cert, leaf_key


# --- 1. ТЕСТЫ ВАЛИДАЦИИ ЦЕПОЧКИ ДОВЕРИЯ X.509 ---

def test_x509_chain_validation_success(ca_and_leaf):
    """Проверяет успешную валидацию цепочки сертификата службы Банка против Root CA."""
    ca_cert, _, leaf_cert, _ = ca_and_leaf
    validator = X509ChainValidator(root_ca_cert=ca_cert)

    is_valid, msg = validator.validate_certificate_chain(leaf_cert)
    assert is_valid is True
    assert "успешно" in msg


def test_x509_chain_validation_rejects_wrong_issuer(ca_and_leaf):
    """Проверяет отклонение сертификата, подписанного чужим CA (Untrusted CA)."""
    _, _, leaf_cert, _ = ca_and_leaf

    # Генерируем совершенно другой корневой CA
    other_ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_ca_name = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Attacker Fake CA"),
        x509.NameAttribute(NameOID.COMMON_NAME, "Fake Root CA"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    other_ca_cert = (
        x509.CertificateBuilder()
        .subject_name(other_ca_name)
        .issuer_name(other_ca_name)
        .public_key(other_ca_key.public_key())
        .serial_number(999)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .sign(other_ca_key, hashes.SHA256())
    )

    validator = X509ChainValidator(root_ca_cert=other_ca_cert)
    is_valid, err = validator.validate_certificate_chain(leaf_cert)
    assert is_valid is False
    assert "Несоответствие цепочки доверия" in err or "эмитент" in err


def test_x509_chain_validation_rejects_expired_cert(ca_and_leaf):
    """Проверяет отклонение сертификата с истекшим сроком действия."""
    ca_cert, ca_key, _, _ = ca_and_leaf
    now = datetime.datetime.now(datetime.timezone.utc)

    expired_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    expired_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "expired-service")]))
        .issuer_name(ca_cert.subject)
        .public_key(expired_key.public_key())
        .serial_number(1002)
        .not_valid_before(now - datetime.timedelta(days=400))
        .not_valid_after(now - datetime.timedelta(days=35))  # Истек месяц назад
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    validator = X509ChainValidator(root_ca_cert=ca_cert)
    is_valid, err = validator.validate_certificate_chain(expired_cert)
    assert is_valid is False
    assert "истек" in err.lower()


def test_x509_chain_validation_rejects_future_cert(ca_and_leaf):
    """Проверяет отклонение сертификата, который еще не вступил в силу."""
    ca_cert, ca_key, _, _ = ca_and_leaf
    now = datetime.datetime.now(datetime.timezone.utc)

    future_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    future_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "future-service")]))
        .issuer_name(ca_cert.subject)
        .public_key(future_key.public_key())
        .serial_number(1003)
        .not_valid_before(now + datetime.timedelta(days=10))  # Вступит в силу через 10 дней
        .not_valid_after(now + datetime.timedelta(days=400))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    validator = X509ChainValidator(root_ca_cert=ca_cert)
    is_valid, err = validator.validate_certificate_chain(future_cert)
    assert is_valid is False
    assert "еще не вступил в силу" in err


# --- 2. ТЕСТЫ ЦИФРОВЫХ ПОДПИСЕЙ И ЗАПРОСОВ ---

def test_x509_signature_verification_rsa_pss(ca_and_leaf):
    """Проверяет валидацию подписи данных RSA-PSS с проверкой сертификата."""
    ca_cert, _, leaf_cert, leaf_key = ca_and_leaf
    validator = X509ChainValidator(root_ca_cert=ca_cert)

    payload = b"financial-operation-payload-to-sign"

    # Подпись приватным ключом leaf
    raw_sig = leaf_key.sign(
        payload,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256()
    )
    sig_b64 = base64.b64encode(raw_sig).decode("ascii")

    leaf_pem = leaf_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")

    # Успешная валидация
    is_valid, msg = validator.verify_signature(payload, sig_b64, leaf_pem)
    assert is_valid is True
    assert "успешно" in msg

    # Подделка данных полезной нагрузки
    is_valid_fake, _ = validator.verify_signature(b"modified-payload", sig_b64, leaf_pem)
    assert is_valid_fake is False


def test_x509_signature_verification_pkcs1v15(ca_and_leaf):
    """Проверяет валидацию подписи PKCS#1 v1.5 (совместимость с аппаратными токенами)."""
    ca_cert, _, leaf_cert, leaf_key = ca_and_leaf
    validator = X509ChainValidator(root_ca_cert=ca_cert)

    payload = b"legacy-hardware-token-payload"
    raw_sig = leaf_key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
    sig_b64 = base64.b64encode(raw_sig).decode("ascii")

    leaf_pem = leaf_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    is_valid, msg = validator.verify_signature(payload, sig_b64, leaf_pem)
    assert is_valid is True
    assert "PKCS1v15" in msg or "успешно" in msg


def test_chain_validator_request_verification(ca_and_leaf):
    """Проверяет комплексную верификацию HTTP-запроса через verify_request."""
    ca_cert, _, leaf_cert, leaf_key = ca_and_leaf
    validator = X509ChainValidator(root_ca_cert=ca_cert, max_drift_seconds=300)

    now_ts = int(time.time())
    nonce = f"test-nonce-{uuid.uuid4()}"
    body = b'{"from_account":"123","to_account":"456","amount":100}'

    canonical_data = validator.compute_canonical_digest(
        timestamp=now_ts,
        nonce=nonce,
        body_bytes=body,
        method="POST",
        path="/api/v1/bank/transfer",
    )

    raw_sig = leaf_key.sign(
        canonical_data,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256()
    )
    sig_b64 = base64.b64encode(raw_sig).decode("ascii")
    leaf_pem = leaf_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")

    # Валидный запрос
    is_valid, msg = validator.verify_request(
        timestamp_val=now_ts,
        nonce=nonce,
        body_bytes=body,
        signature_b64=sig_b64,
        leaf_cert_pem=leaf_pem,
        method="POST",
        path="/api/v1/bank/transfer",
    )
    assert is_valid is True

    # Запрос с устаревшей временной меткой
    is_valid_stale, stale_msg = validator.verify_request(
        timestamp_val=now_ts - 500,
        nonce=nonce,
        body_bytes=body,
        signature_b64=sig_b64,
        leaf_cert_pem=leaf_pem,
        method="POST",
        path="/api/v1/bank/transfer",
    )
    assert is_valid_stale is False
    assert "устарела" in stale_msg


# --- 3. ТЕСТЫ ИНТЕГРАЦИИ С BANK CA И ЧЕКАМИ ---

def test_bank_ca_central_pki_loading(tmp_path, monkeypatch):
    """Проверяет корректность загрузки и валидации сертификатов из центрального Root CA."""
    from src.pki.core.ca_engine import RootCAEngine
    from src.pki.core.cert_issuer import CertificateIssuer

    ca_dir = tmp_path / "ca"
    out_dir = tmp_path / "certs"

    # Генерируем Root CA
    out_dir.mkdir(parents=True, exist_ok=True)
    root_engine = RootCAEngine(data_dir=str(ca_dir), passphrase="TestSecurePassphrase123!")
    root_engine.ensure_initialized()
    root_engine.export_ca_certificate(str(out_dir / "ca.crt"))

    # Выпускаем сертификат Банка
    issuer = CertificateIssuer(root_engine)
    bank_cert_obj, bank_key_obj = issuer.issue_certificate(
        common_name="bank-simulation",
        role="bank_service",
        san_dns_names=["simulation-api", "localhost"],
        san_ip_addresses=["127.0.0.1"],
        is_server=True,
        is_client=True,
    )
    issuer.save_cert_and_key(bank_cert_obj, bank_key_obj, out_dir / "bank.crt", out_dir / "bank.key")

    # Настраиваем окружение
    monkeypatch.setenv("ROOT_CA_CERT_PATH", str(out_dir / "ca.crt"))
    monkeypatch.setenv("BANK_CERT_PATH", str(out_dir / "bank.crt"))
    monkeypatch.setenv("BANK_KEY_PATH", str(out_dir / "bank.key"))

    loaded_ca = BankCertificateAuthority.load_from_central_pki()
    assert loaded_ca is not None
    assert loaded_ca.ca_cert is not None
    assert loaded_ca.bank_cert is not None
    assert loaded_ca.bank_private_key is not None
    # Приватный ключ Root CA не должен быть загружен в Банк (изоляция Zero-Trust!)
    assert loaded_ca.ca_private_key is None

    # Проверяем валидность цепочки
    assert BankCertificateAuthority.verify_certificate_against_ca(
        loaded_ca.get_bank_cert_pem(),
        loaded_ca.get_ca_cert_pem()
    ) is True


def test_receipt_signer_contains_bank_cert_serial(ca_and_leaf):
    """Проверяет, что сформированный чек операции содержит серийный номер сертификата банка."""
    ca_cert, ca_key, leaf_cert, leaf_key = ca_and_leaf
    ca_instance = BankCertificateAuthority(
        ca_cert=ca_cert,
        ca_private_key=ca_key,
        bank_cert=leaf_cert,
        bank_private_key=leaf_key,
    )
    signer = ReceiptSigner(ca=ca_instance)

    receipt = signer.create_and_sign_receipt(
        action="test_action",
        data={"amount": 100},
        request_nonce="nonce-123",
    )

    assert "bank_cert_serial" in receipt
    assert receipt["bank_cert_serial"] == str(leaf_cert.serial_number)
    assert receipt["bank_cert_serial"] == "1001"

    # Проверка валидности чека
    is_valid, _ = ReceiptSigner.verify_receipt(receipt)
    assert is_valid is True
