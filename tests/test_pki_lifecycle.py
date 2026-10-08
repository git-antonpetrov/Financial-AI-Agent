"""
Комплексные тесты жизненного цикла PKI (Phase 7: test_pki_lifecycle.py).
Проверяет выпуск, валидацию X.509 расширений, цепочек доверия, изоляцию ключей CA и отзыв сертификатов (CRL).
"""

import os
import sys
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import ExtendedKeyUsageOID
from fastapi import HTTPException, Request

# Моки внешних сервисов
for mod in ["redis", "minio", "litellm", "asyncpg", "chromadb", "psycopg2"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

# Пути
sys.path.insert(0, os.path.abspath("src/pki"))
sys.path.insert(0, os.path.abspath("src/admin_server"))
sys.path.insert(0, os.path.abspath("src/admin_server/fastapi"))
sys.path.insert(0, os.path.abspath("src/simulations/core/crypto"))

from src.pki.bootstrap_pki import bootstrap_pki
from src.pki.core.ca_engine import RootCAEngine
from src.pki.core.cert_issuer import CertificateIssuer
from src.simulations.core.crypto.chain_validator import X509ChainValidator
from src.simulations.core.crypto.bank_ca import BankCertificateAuthority
import security
from security import (
    verify_mtls_client_identity,
    revoke_certificate_in_redis,
    is_certificate_revoked_in_redis,
    _revoked_certs_memory,
)


@pytest.fixture(autouse=True)
def clean_revoked_memory():
    """Очищает локальный in-memory кэш отозванных сертификатов перед каждым тестом."""
    _revoked_certs_memory.clear()
    yield
    _revoked_certs_memory.clear()


@pytest.fixture
def pki_test_env(tmp_path):
    """Инициализирует тестовое изолированное окружение PKI."""
    data_dir = tmp_path / "ca_data"
    export_dir = tmp_path / "certs_export"
    passphrase = "StrictProductionPassphrase_2026_Test!"

    manifest = bootstrap_pki(
        data_dir=data_dir,
        export_dir=export_dir,
        passphrase=passphrase,
        force=True
    )
    return {
        "data_dir": data_dir,
        "export_dir": export_dir,
        "passphrase": passphrase,
        "manifest": manifest
    }


def test_pki_bootstrap_full_lifecycle(pki_test_env):
    """Проверяет полный цикл bootstrap PKI: наличие корневого CA, всех сервисных сертификатов и MinIO структуры."""
    export_dir = pki_test_env["export_dir"]
    manifest = pki_test_env["manifest"]

    assert (export_dir / "ca.crt").exists()
    assert (pki_test_env["data_dir"] / "ca.key").exists()

    service_files = ["admin_server", "admin_client", "bank", "postgres", "redis", "minio"]
    for svc in service_files:
        assert (export_dir / f"{svc}.crt").exists()
        assert (export_dir / f"{svc}.key").exists()

    expected_manifest_services = ["admin-server", "admin-client", "bank", "postgres", "redis", "minio"]
    for svc in expected_manifest_services:
        assert svc in manifest["services"]
        assert manifest["services"][svc]["status"] == "ISSUED"

    # Проверка структуры MinIO
    assert (export_dir / "minio" / "public.crt").exists()
    assert (export_dir / "minio" / "private.key").exists()
    assert (export_dir / "minio" / "CAs" / "ca.crt").exists()


def test_pki_x509_extensions_and_key_usage(pki_test_env):
    """Проверяет корректность расширений X.509 v3: BasicConstraints, KeyUsage и ExtendedKeyUsage."""
    export_dir = pki_test_env["export_dir"]

    # 1. Корневой сертификат (Root CA)
    ca_bytes = (export_dir / "ca.crt").read_bytes()
    ca_cert = x509.load_pem_x509_certificate(ca_bytes)

    bc = ca_cert.extensions.get_extension_for_oid(x509.ExtensionOID.BASIC_CONSTRAINTS).value
    assert bc.ca is True

    ca_ku = ca_cert.extensions.get_extension_for_oid(x509.ExtensionOID.KEY_USAGE).value
    assert ca_ku.key_cert_sign is True
    assert ca_ku.crl_sign is True

    # 2. Серверный сертификат (admin_server.crt)
    server_bytes = (export_dir / "admin_server.crt").read_bytes()
    server_cert = x509.load_pem_x509_certificate(server_bytes)
    server_bc = server_cert.extensions.get_extension_for_oid(x509.ExtensionOID.BASIC_CONSTRAINTS).value
    assert server_bc.ca is False

    server_eku = server_cert.extensions.get_extension_for_oid(x509.ExtensionOID.EXTENDED_KEY_USAGE).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in server_eku

    # 3. Клиентский сертификат (admin_client.crt)
    client_bytes = (export_dir / "admin_client.crt").read_bytes()
    client_cert = x509.load_pem_x509_certificate(client_bytes)
    client_eku = client_cert.extensions.get_extension_for_oid(x509.ExtensionOID.EXTENDED_KEY_USAGE).value
    assert ExtendedKeyUsageOID.CLIENT_AUTH in client_eku


def test_pki_chain_validation_across_all_services(pki_test_env):
    """Проверяет валидацию цепочки доверия X.509 всех выпущенных сертификатов по Root CA."""
    export_dir = pki_test_env["export_dir"]
    ca_cert_path = str(export_dir / "ca.crt")
    validator = X509ChainValidator(ca_cert_path=ca_cert_path)

    for svc in ["admin_server", "admin_client", "bank", "postgres", "redis", "minio"]:
        cert_path = str(export_dir / f"{svc}.crt")
        valid, msg = validator.validate_certificate_chain(cert_path)
        assert valid is True, f"Сертификат {svc} не прошел валидацию цепочки: {msg}"


def test_pki_crl_revocation_blocks_access(pki_test_env):
    """Проверяет сценарий отзыва сертификата (CRL) и блокировку доступа через verify_mtls_client_identity."""
    export_dir = pki_test_env["export_dir"]
    client_cert_path = export_dir / "admin_client.crt"
    client_cert = x509.load_pem_x509_certificate(client_cert_path.read_bytes())
    serial = str(client_cert.serial_number)

    # 1. До отзыва сертификат не заблокирован
    assert is_certificate_revoked_in_redis(serial) is False

    # 2. Отзываем сертификат
    revoke_certificate_in_redis(serial, reason="key_compromise")
    assert is_certificate_revoked_in_redis(serial) is True

    # 3. verify_mtls_client_identity блокирует запрос с отозванным серийным номером (HTTP 401)
    req = Request({
        "type": "http",
        "method": "POST",
        "url": "http://testserver/api/v1/rag/documents",
        "headers": [
            (b"x-client-cert-verify", b"SUCCESS"),
            (b"x-client-cert-subject", b"CN=superadmin-workstation"),
            (b"x-client-cert-serial", serial.encode("utf-8")),
        ]
    })

    with pytest.raises(HTTPException) as exc_info:
        verify_mtls_client_identity(req)
    assert exc_info.value.status_code == 401
    assert "отозван" in exc_info.value.detail.lower()


def test_pki_zero_trust_key_isolation(pki_test_env):
    """Проверяет изоляцию ключа Root CA: сервисы загружают только свой ключ и ca.crt, без доступа к ca.key."""
    export_dir = pki_test_env["export_dir"]

    # Банк загружает сертификаты из центрального PKI
    bank_ca = BankCertificateAuthority.load_from_central_pki(
        ca_cert_path=str(export_dir / "ca.crt"),
        bank_cert_path=str(export_dir / "bank.crt"),
        bank_key_path=str(export_dir / "bank.key")
    )
    # Приватный ключ Root CA строго недоступен банковскому сервису
    assert bank_ca.ca_private_key is None
    assert bank_ca.bank_private_key is not None
    assert bank_ca.bank_cert is not None
