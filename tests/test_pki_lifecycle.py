"""
Комплексные тесты жизненного цикла PKI (Phase 7: test_pki_lifecycle.py).
Проверяет выпуск, валидацию X.509 расширений, цепочек доверия, изоляцию ключей CA и отзыв сертификатов (CRL).
"""

import datetime
import os
import sys
from unittest.mock import MagicMock
import pytest

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
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

from src.pki.bootstrap_pki import PUBLIC_ADMIN_DNS_NAME, bootstrap_pki, certificate_remains_usable
from src.simulations.core.crypto.chain_validator import X509ChainValidator
from src.simulations.core.crypto.bank_ca import BankCertificateAuthority
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
def pki_test_env(tmp_path, monkeypatch):
    """Инициализирует тестовое изолированное окружение PKI."""
    monkeypatch.delenv("BANK_CERTS_DIR", raising=False)
    monkeypatch.delenv("ADMIN_CERTS_DIR", raising=False)
    monkeypatch.delenv("ADMIN_CLIENT_CERTS_DIR", raising=False)
    monkeypatch.delenv("CA_PUBLIC_DIR", raising=False)
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


def test_bootstrap_sets_mode_0600_on_private_keys(tmp_path, monkeypatch):
    """Проверяет, что после copyfile каждый закрытый ключ получает режим 0600."""
    import sys

    bootstrap_module = sys.modules["src.pki.bootstrap_pki"]
    recorded: list[tuple[str, int]] = []
    original_chmod = bootstrap_module.os.chmod

    def spy_chmod(path, mode):
        recorded.append((os.fspath(path), mode))
        return original_chmod(path, mode)

    monkeypatch.setattr(bootstrap_module.os, "chmod", spy_chmod)
    for env_name in (
        "BANK_CERTS_DIR",
        "ADMIN_CERTS_DIR",
        "ADMIN_CLIENT_CERTS_DIR",
        "CA_PUBLIC_DIR",
        "POSTGRES_CERTS_DIR",
        "REDIS_CERTS_DIR",
        "MINIO_CERTS_DIR",
        "ENCLAVE_CERTS_DIR",
    ):
        monkeypatch.delenv(env_name, raising=False)

    export_dir = tmp_path / "certs_export"
    bootstrap_pki(
        data_dir=tmp_path / "ca_data",
        export_dir=export_dir,
        passphrase="StrictProductionPassphrase_2026_Test!",
        force=True,
    )

    key_records = [(path, mode) for path, mode in recorded if path.endswith(".key")]
    assert key_records
    assert {mode for _, mode in key_records} == {0o600}
    normalized = [path.replace("\\", "/") for path, _ in key_records]
    assert any(path.endswith("minio/private.key") for path in normalized)
    assert (export_dir / "minio" / "private.key").is_file()


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
    server_san = server_cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert PUBLIC_ADMIN_DNS_NAME in server_san.get_values_for_type(x509.DNSName)
    admin_dir = export_dir / "admin_certs"
    assert (admin_dir / "admin_server.crt").exists()
    assert (admin_dir / "admin_server.key").exists()
    assert not (admin_dir / "admin_client.key").exists()
    assert not (admin_dir / "bank.key").exists()

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
            (b"x-proxy-secret", os.environ["PROXY_INTERNAL_SECRET"].encode()),
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


def _self_signed_certificate(dns_names: list[str], days: int) -> x509.Certificate:
    """Выпускает самоподписанный сертификат для проверки решения о перевыпуске."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "admin-server")])
    now = datetime.datetime.now(datetime.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=days))
    )
    if dns_names:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(item) for item in dns_names]),
            critical=False,
        )
    return builder.sign(key, hashes.SHA256())


def test_certificate_without_public_name_is_reissued():
    """Проверяет, что сертификат без публичного имени администратора не остаётся в обороте."""
    now = datetime.datetime.now(datetime.UTC)
    missing_name = _self_signed_certificate(["admin-server", "localhost"], days=200)
    assert certificate_remains_usable(missing_name, [PUBLIC_ADMIN_DNS_NAME, "localhost"], now) is False

    covered = _self_signed_certificate([PUBLIC_ADMIN_DNS_NAME, "localhost"], days=200)
    assert certificate_remains_usable(covered, [PUBLIC_ADMIN_DNS_NAME], now) is True

    expiring = _self_signed_certificate([PUBLIC_ADMIN_DNS_NAME], days=10)
    assert certificate_remains_usable(expiring, [PUBLIC_ADMIN_DNS_NAME], now) is False


def test_admin_certificate_with_public_name_is_reused(pki_test_env):
    """Проверяет, что повторный выпуск оставляет сертификат с публичным именем."""
    second = bootstrap_pki(
        data_dir=pki_test_env["data_dir"],
        export_dir=pki_test_env["export_dir"],
        passphrase=pki_test_env["passphrase"],
        force=False,
    )
    first_serial = pki_test_env["manifest"]["services"]["admin-server"]["serial"]
    assert second["services"]["admin-server"]["status"] == "VALID"
    assert second["services"]["admin-server"]["serial"] == first_serial


def test_replaced_client_certificate_is_rejected_by_crl(pki_test_env, monkeypatch):
    """Проверяет, что заменённый клиентский сертификат попадает в CRL и больше не проходит проверку."""
    export_dir = pki_test_env["export_dir"]
    old_bytes = (export_dir / "admin_client.crt").read_bytes()
    old_cert = x509.load_pem_x509_certificate(old_bytes)
    (export_dir / "admin_client.key").unlink()
    bootstrap_pki(
        data_dir=pki_test_env["data_dir"],
        export_dir=export_dir,
        passphrase=pki_test_env["passphrase"],
        force=False,
    )

    revocation_list = x509.load_pem_x509_crl((export_dir / "ca.crl").read_bytes())
    assert revocation_list.get_revoked_certificate_by_serial_number(old_cert.serial_number) is not None
    assert (export_dir / "admin_certs" / "ca.crl").is_file()
    assert (export_dir / "bank_certs" / "ca.crl").is_file()

    validator = X509ChainValidator(ca_cert_path=export_dir / "ca.crt", crl_path=export_dir / "ca.crl")
    rejected, _message = validator.validate_certificate_chain(old_bytes)
    assert rejected is False
    accepted, message = validator.validate_certificate_chain(export_dir / "admin_client.crt")
    assert accepted is True, message

    monkeypatch.setenv("CRL_PATH", str(export_dir / "ca.crl"))
    monkeypatch.setenv("ROOT_CA_CERT_PATH", str(export_dir / "ca.crt"))
    monkeypatch.setenv("STRICT_SECURITY", "false")
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "url": "http://testserver/api/health",
            "headers": [
                (b"x-proxy-secret", os.environ["PROXY_INTERNAL_SECRET"].encode()),
                (b"x-client-cert-verify", b"SUCCESS"),
                (b"x-client-cert-subject", b"CN=superadmin-workstation"),
                (b"x-client-cert-serial", str(old_cert.serial_number).encode("utf-8")),
            ],
        }
    )
    with pytest.raises(HTTPException) as revoked:
        verify_mtls_client_identity(request)
    assert revoked.value.status_code == 401


def test_certificate_revoke_fails_closed_without_redis(monkeypatch):
    """Не подтверждает отзыв сертификата, если строгий режим не записал его в Redis."""

    class BrokenRedis:
        def setex(self, *_args, **_kwargs):
            raise ConnectionError("redis down")

        def publish(self, *_args, **_kwargs):
            return 1

    monkeypatch.setenv("STRICT_SECURITY", "true")
    monkeypatch.setattr("security.redis_blacklist", None)
    with pytest.raises(HTTPException) as missing:
        revoke_certificate_in_redis(serial="5150")
    assert missing.value.status_code == 503

    with pytest.raises(HTTPException) as broken:
        revoke_certificate_in_redis(serial="5151", redis_conn=BrokenRedis())
    assert broken.value.status_code == 503


def test_assign_private_key_owner_sets_service_uid(tmp_path, monkeypatch):
    """Отдаёт закрытые ключи выделенного тома пользователю целевого сервиса."""
    from pathlib import Path

    bootstrap_module = sys.modules["src.pki.bootstrap_pki"]

    key_file = tmp_path / "bank.key"
    cert_file = tmp_path / "bank.crt"
    key_file.write_text("key", encoding="utf-8")
    cert_file.write_text("cert", encoding="utf-8")
    recorded: list[tuple[str, int, int]] = []
    monkeypatch.setattr(bootstrap_module.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(
        bootstrap_module.os,
        "chown",
        lambda path, uid, gid: recorded.append((Path(path).name, uid, gid)),
        raising=False,
    )

    bootstrap_module.assign_private_key_owner(tmp_path, 10001, 10001)

    assert recorded == [("bank.key", 10001, 10001)]
    assert bootstrap_module.KEY_OWNER_BY_DIRECTORY["BANK_CERTS_DIR"] == (10001, 10001)
    assert bootstrap_module.KEY_OWNER_BY_DIRECTORY["ADMIN_CERTS_DIR"] == (10001, 10001)
    assert bootstrap_module.KEY_OWNER_BY_DIRECTORY["REDIS_CERTS_DIR"] == (999, 1000)
