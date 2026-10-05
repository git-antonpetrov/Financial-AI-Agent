import os
import time
import base64
import json
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes, serialization

from src.simulations.api.main import app
from src.simulations.core.crypto.bank_ca import BankCertificateAuthority, get_bank_ca
from src.simulations.core.crypto.receipt_signer import ReceiptSigner, get_receipt_signer
from src.simulations.core.crypto.enclave_verifier import (
    NonceStore,
    EnclaveKeyManager,
    EnclaveVerifier,
    get_enclave_verifier,
)
from src.simulations.core.crypto.oracle_verifier import OracleVerifier, get_oracle_verifier
from src.simulations.core.crypto.platform_signer import DigitalPlatformSigner, get_digital_platform_signer
from src.simulations.db.digital_ruble.db.models import SmartContract


@pytest.fixture(autouse=True)
def setup_test_env(monkeypatch):
    monkeypatch.setenv("AGENT_BANK_BOOTSTRAP_TOKEN", "test_bank_token")
    monkeypatch.setenv("AGENT_INVEST_BOOTSTRAP_TOKEN", "test_invest_token")
    monkeypatch.setenv("AGENT_DIGITAL_BOOTSTRAP_TOKEN", "test_digital_token")
    monkeypatch.setenv("ORACLE_BOOTSTRAP_TOKEN", "test_oracle_token")


# =========================================================================
# 1. Тесты Центра Сертификации Банка (BankCertificateAuthority)
# =========================================================================

def test_bank_ca_generation_and_validation():
    """Проверка генерации Root CA и сертификата Банка, а также валидации цепочки доверия."""
    ca = BankCertificateAuthority.create_in_memory()
    assert ca.ca_cert is not None
    assert ca.bank_cert is not None

    # Валидация цепочки доверия x509
    assert BankCertificateAuthority.verify_certificate_against_ca(
        ca.get_bank_cert_pem(),
        ca.get_ca_cert_pem(),
    ) is True

    # Проверка субъектов и издателей
    assert "Financial AI Agent Root CA" in ca.ca_cert.subject.rfc4514_string()
    assert "Financial AI Agent Root CA" in ca.bank_cert.issuer.rfc4514_string()
    assert "Financial AI Simulation Bank" in ca.bank_cert.subject.rfc4514_string()

    # PEM представление
    ca_pem = ca.get_ca_cert_pem()
    bank_pem = ca.get_bank_cert_pem()
    assert ca_pem.startswith("-----BEGIN CERTIFICATE-----")
    assert bank_pem.startswith("-----BEGIN CERTIFICATE-----")

    # Отпечаток SHA-256
    fingerprint = ca.get_bank_cert_fingerprint()
    assert len(fingerprint) == 64
    assert all(c in "0123456789abcdef" for c in fingerprint.lower())


# =========================================================================
# 2. Тесты подписи и валидации чеков (ReceiptSigner)
# =========================================================================

def test_receipt_signing_and_verification():
    """Проверка формирования и криптографической проверки чека-квитанции приватным ключом Банка."""
    ca = BankCertificateAuthority.create_in_memory()
    signer = ReceiptSigner(ca=ca)

    sample_data = {
        "operation": "transfer",
        "from_account": "acc_001",
        "to_account": "acc_002",
        "amount": "15000.50",
    }
    signed_receipt = signer.create_and_sign_receipt(
        action="bank_transfer",
        data=sample_data,
        request_nonce="nonce-abc-123",
    )

    assert "receipt" in signed_receipt
    assert "signature" in signed_receipt
    assert "bank_cert_pem" in signed_receipt
    assert signed_receipt["receipt"]["action"] == "bank_transfer"
    assert signed_receipt["receipt"]["request_nonce"] == "nonce-abc-123"

    # Успешная верификация чека
    is_valid, msg = signer.verify_receipt(signed_receipt)
    assert is_valid is True
    assert "успешно" in msg

    # Подделка данных чека
    tampered_receipt = dict(signed_receipt)
    tampered_receipt["receipt"] = dict(signed_receipt["receipt"])
    tampered_receipt["receipt"]["data_hash"] = "0" * 64
    is_valid, err = signer.verify_receipt(tampered_receipt)
    assert is_valid is False
    assert "Недействительная цифровая подпись" in err

    # Подделка подписи
    tampered_sig = dict(signed_receipt)
    fake_sig = base64.b64encode(b"invalid_signature_bytes_here_123456").decode("ascii")
    tampered_sig["signature"] = fake_sig
    is_valid, err = signer.verify_receipt(tampered_sig)
    assert is_valid is False


# =========================================================================
# 3. Тесты предотвращения Replay-атак (NonceStore)
# =========================================================================

def test_nonce_store_replay_prevention():
    """Проверка одноразового использования nonce для защиты от атак повторного воспроизведения."""
    store = NonceStore(default_ttl=10)
    nonce_test = "test-nonce-single-use-001"

    # Первый вызов — успешно
    assert store.record_nonce_if_new(nonce_test) is True

    # Повторный вызов с тем же nonce — отклоняется (Replay Attack)
    assert store.record_nonce_if_new(nonce_test) is False

    # Другой nonce — успешно
    assert store.record_nonce_if_new("test-nonce-single-use-002") is True


# =========================================================================
# 4. Тесты проверки подписей Анклава (EnclaveVerifier)
# =========================================================================

def test_enclave_verifier_request_validation():
    """Проверка криптографической аутентификации запросов от Анклава (Gramine)."""
    key_mgr = EnclaveKeyManager()
    verifier = EnclaveVerifier(key_manager=key_mgr, nonce_store=NonceStore(default_ttl=60))

    method = "POST"
    path = "/bank/transfers"
    now_ts = int(time.time())
    nonce = f"nonce-{now_ts}-1"
    body = b'{"from_account_id":"acc_1","to_account_id":"acc_2","amount":100}'

    canonical_data = verifier.compute_payload_digest(
        method=method,
        path=path,
        timestamp=now_ts,
        nonce=nonce,
        body_bytes=body,
    )

    # Подписываем приватным ключом Анклава (RSA-PSS)
    dev_priv_key = key_mgr._dev_private_key
    raw_sig = dev_priv_key.sign(
        canonical_data,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    sig_b64 = base64.b64encode(raw_sig).decode("ascii")

    # Успешная проверка подписи
    is_valid, msg = verifier.verify_request(
        method=method,
        path=path,
        timestamp_val=now_ts,
        nonce=nonce,
        body_bytes=body,
        signature_b64=sig_b64,
    )
    assert is_valid is True
    assert "валидна" in msg

    # Попытка Replay Attack с тем же nonce
    is_valid_replay, err_replay = verifier.verify_request(
        method=method,
        path=path,
        timestamp_val=now_ts,
        nonce=nonce,
        body_bytes=body,
        signature_b64=sig_b64,
    )
    assert is_valid_replay is False
    assert "Replay Attack" in err_replay

    # Проверка отклонения устаревшего timestamp (Time Skew)
    expired_ts = now_ts - 700
    is_valid_skew, err_skew = verifier.verify_request(
        method=method,
        path=path,
        timestamp_val=expired_ts,
        nonce="nonce-skew-001",
        body_bytes=body,
        signature_b64=sig_b64,
    )
    assert is_valid_skew is False
    assert "устарел" in err_skew


# =========================================================================
# 5. Тесты Оракула цифрового рубля (OracleVerifier)
# =========================================================================

def test_oracle_verifier_signature():
    """Проверка создания и валидации цифровой подписи доверенного оракула."""
    verifier = OracleVerifier()
    contract_id = "contract-oracle-777"
    status = "fulfilled"
    oracle_name = "trusted_rate_oracle"

    sig = verifier.sign_condition(
        contract_id=contract_id,
        status=status,
        oracle_name=oracle_name,
    )
    assert len(sig) > 0

    # Проверка подлинной подписи
    is_valid, msg = verifier.verify_condition_signature(
        contract_id=contract_id,
        status=status,
        oracle_name=oracle_name,
        signature_b64=sig,
    )
    assert is_valid is True
    assert "валидна" in msg

    # Проверка поддельного условия (подделка статуса на failed)
    is_valid_fake, err_fake = verifier.verify_condition_signature(
        contract_id=contract_id,
        status="failed",
        oracle_name=oracle_name,
        signature_b64=sig,
    )
    assert is_valid_fake is False
    assert "недействительна" in err_fake.lower()


# =========================================================================
# 6. Тесты платформенного подписанта (DigitalPlatformSigner)
# =========================================================================

def test_digital_platform_signer():
    """Проверка подписания транзакций платформы цифрового рубля."""
    signer = DigitalPlatformSigner()
    ts = int(time.time())
    sig = signer.sign_transaction(
        sender_id="w_sender",
        receiver_id="w_receiver",
        amount=Decimal("500.00"),
        contract_id="contract_direct",
        timestamp=ts,
    )
    assert len(sig) > 0

    # Верификация корректной транзакции
    assert signer.verify_transaction_signature(
        sender_id="w_sender",
        receiver_id="w_receiver",
        amount=Decimal("500.00"),
        contract_id="contract_direct",
        timestamp=ts,
        signature_b64=sig,
    ) is True

    # Модификация суммы -> сбой проверки
    assert signer.verify_transaction_signature(
        sender_id="w_sender",
        receiver_id="w_receiver",
        amount=Decimal("500.01"),
        contract_id="contract_direct",
        timestamp=ts,
        signature_b64=sig,
    ) is False


# =========================================================================
# 7. Тесты публичных эндпоинтов PKI Банка
# =========================================================================

def test_bank_pki_endpoints():
    """Проверка публичных эндпоинтов распространения сертификатов Root CA и Банка."""
    client = TestClient(app)

    # 1. Сводная информация о сертификатах
    res_info = client.get("/bank/ca/certificate")
    assert res_info.status_code == 200
    data = res_info.json()
    assert "root_ca_pem" in data
    assert "bank_cert_pem" in data
    assert "bank_fingerprint" in data
    assert data["root_ca_pem"].startswith("-----BEGIN CERTIFICATE-----")
    assert data["bank_cert_pem"].startswith("-----BEGIN CERTIFICATE-----")

    # 2. Сертификат Root CA в PEM
    res_root = client.get("/bank/ca/root-cert.pem")
    assert res_root.status_code == 200
    assert res_root.headers["content-type"].startswith("text/plain")
    assert res_root.text.startswith("-----BEGIN CERTIFICATE-----")

    # 3. Сертификат Банка в PEM
    res_bank = client.get("/bank/ca/bank-cert.pem")
    assert res_bank.status_code == 200
    assert res_bank.headers["content-type"].startswith("text/plain")
    assert res_bank.text.startswith("-----BEGIN CERTIFICATE-----")


# =========================================================================
# 8. Тесты эндпоинта оракула с цифровой подписью
# =========================================================================

def test_oracle_endpoint_with_valid_and_invalid_signature(monkeypatch):
    """Проверка криптографической валидации подписи оракула в роутере цифрового рубля."""
    client = TestClient(app)

    fake_contract = SmartContract(
        id="contract-sec-1",
        creator_wallet_id="w1",
        receiver_wallet_id="w2",
        amount=Decimal("100"),
        condition_type="oracle",
        contract_code="standard",
        status="active",
        condition_status="pending",
    )

    mock_session = AsyncMock()
    mock_session.scalar.return_value = fake_contract
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()

    mock_cm = MagicMock()
    mock_cm.__aenter__.return_value = mock_session
    mock_cm.__aexit__.return_value = None

    monkeypatch.setattr("src.simulations.api.routers.ruble.ruble_db", lambda: mock_cm)

    # Формируем валидную подпись оракула
    sig = get_oracle_verifier().sign_condition(
        contract_id="contract-sec-1",
        status="fulfilled",
        oracle_name="rate_oracle",
    )

    # 1. Запрос с валидной подписью оракула
    res_valid = client.post(
        "/ruble/smart-contracts/contract-sec-1/condition",
        headers={"X-Bootstrap-Token": "test_oracle_token"},
        json={
            "status": "fulfilled",
            "oracle_name": "rate_oracle",
            "signature": sig,
        },
    )
    assert res_valid.status_code == 200
    res_data = res_valid.json()
    assert res_data["status"] == "success"
    assert "signed_receipt" in res_data
    assert res_valid.headers.get("X-Bank-Signature") is not None

    # Верификация полученного от Банка чека
    is_receipt_valid, _ = get_receipt_signer().verify_receipt(res_data["signed_receipt"])
    assert is_receipt_valid is True

    # 2. Запрос с недействительной подписью оракула -> 403 Forbidden
    fake_sig = base64.b64encode(b"invalid_oracle_signature_123456").decode("ascii")
    res_invalid = client.post(
        "/ruble/smart-contracts/contract-sec-1/condition",
        headers={"X-Bootstrap-Token": "test_oracle_token"},
        json={
            "status": "fulfilled",
            "oracle_name": "rate_oracle",
            "signature": fake_sig,
        },
    )
    assert res_invalid.status_code == 403
    assert "Недействительная цифровая подпись оракула" in res_invalid.json()["detail"]


# =========================================================================
# 9. Интеграционный тест: Запрос с подписью Анклава и защита от Replay
# =========================================================================

def test_enclave_signed_request_and_replay_protection():
    """
    Интеграционный тест: отправка запроса в API с криптографической
    подписью Анклава, проверка ответа и отклонение повторного запроса (Replay Attack).
    """
    client = TestClient(app)

    verifier = get_enclave_verifier()
    dev_priv_key = verifier.key_manager._dev_private_key

    method = "GET"
    path = "/invest/products/terms"
    now_ts = int(time.time())
    nonce = f"enclave-nonce-{now_ts}"
    body = b""

    canonical_data = verifier.compute_payload_digest(
        method=method,
        path=path,
        timestamp=now_ts,
        nonce=nonce,
        body_bytes=body,
    )

    raw_sig = dev_priv_key.sign(
        canonical_data,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    sig_b64 = base64.b64encode(raw_sig).decode("ascii")

    headers = {
        "X-Enclave-Signature": sig_b64,
        "X-Nonce": nonce,
        "X-Timestamp": str(now_ts),
    }

    # 1. Первый запрос с подписью Анклава должен пройти успешно
    res1 = client.get(path, headers=headers)
    assert res1.status_code == 200
    assert "key_rate" in res1.json()

    # 2. Повторный запрос с ТЕМ ЖЕ nonce должен быть заблокирован как Replay Attack (409 Conflict)
    res2 = client.get(path, headers=headers)
    assert res2.status_code == 409
    assert "Replay Attack" in res2.json()["detail"]
