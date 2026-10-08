import datetime
import os
from pathlib import Path
import pytest
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.pki.core.passphrase_vault import resolve_root_ca_passphrase
from src.pki.core.ca_engine import RootCAEngine
from src.pki.core.cert_issuer import CertificateIssuer
from src.pki.bootstrap_pki import bootstrap_pki


def test_passphrase_vault_env_resolution(monkeypatch):
    """Проверяет получение и валидацию парольной фразы из переменных окружения."""
    monkeypatch.setenv("ROOT_CA_PASSPHRASE", "my-custom-strong-passphrase-999")
    passphrase = resolve_root_ca_passphrase()
    assert passphrase == "my-custom-strong-passphrase-999"


def test_passphrase_vault_auto_generation(monkeypatch):
    """Проверяет автогенерацию стойкой парольной фразы при ее отсутствии в dev/test режиме."""
    monkeypatch.delenv("ROOT_CA_PASSPHRASE", raising=False)
    monkeypatch.setenv("STRICT_SECURITY", "false")
    monkeypatch.delenv("ENV", raising=False)
    passphrase = resolve_root_ca_passphrase()
    assert len(passphrase) >= 24


def test_passphrase_vault_strict_mode_rejection(monkeypatch):
    """Проверяет, что в production/strict режиме отсутствие фразы вызывает ошибку."""
    monkeypatch.delenv("ROOT_CA_PASSPHRASE", raising=False)
    monkeypatch.setenv("STRICT_SECURITY", "true")
    with pytest.raises(RuntimeError, match="ROOT_CA_PASSPHRASE не задана"):
        resolve_root_ca_passphrase()


def test_root_ca_engine_initialization_and_encryption(tmp_path: Path):
    """
    Проверяет генерацию 4096-битной пары ключей Root CA,
    стойкое шифрование ca.key парольной фразой и параметры ca.crt.
    """
    passphrase = "test-secret-ca-passphrase-4096"
    engine = RootCAEngine(data_dir=tmp_path, passphrase=passphrase, key_size=2048, validity_years=10)
    
    cert, priv_key = engine.ensure_initialized()
    
    assert (tmp_path / "ca.key").exists()
    assert (tmp_path / "ca.crt").exists()
    assert (tmp_path / "serial.txt").exists()

    # 1. Проверяем, что приватный ключ зашифрован на диске и не читается без пароля
    raw_key_bytes = (tmp_path / "ca.key").read_bytes()
    with pytest.raises(TypeError):
        serialization.load_pem_private_key(raw_key_bytes, password=None, backend=default_backend())

    # С верным паролем ключ успешно загружается
    loaded_key = serialization.load_pem_private_key(raw_key_bytes, password=passphrase.encode(), backend=default_backend())
    assert isinstance(loaded_key, rsa.RSAPrivateKey)

    # 2. Проверяем свойства самоподписанного корневого сертификата
    assert cert.subject == cert.issuer
    assert cert.serial_number == 1
    
    bc = cert.extensions.get_extension_for_oid(x509.ExtensionOID.BASIC_CONSTRAINTS).value
    assert bc.ca is True

    ku = cert.extensions.get_extension_for_oid(x509.ExtensionOID.KEY_USAGE).value
    assert ku.key_cert_sign is True
    assert ku.crl_sign is True

    # 3. Проверяем самоподпись математически
    cert.public_key().verify(
        cert.signature,
        cert.tbs_certificate_bytes,
        padding.PKCS1v15(),
        cert.signature_hash_algorithm,
    )


def test_serial_number_increment(tmp_path: Path):
    """Проверяет корректность атомарного инкремента счетчика в serial.txt."""
    engine = RootCAEngine(data_dir=tmp_path, passphrase="test-passphrase", key_size=2048)
    engine.ensure_initialized()

    s1 = engine.get_next_serial()
    s2 = engine.get_next_serial()
    s3 = engine.get_next_serial()

    assert s1 == 2
    assert s2 == 3
    assert s3 == 4
    assert (tmp_path / "serial.txt").read_text(encoding="utf-8").strip() == "5"


def test_cert_issuer_service_certificate(tmp_path: Path):
    """
    Проверяет выпуск сертификата сервиса (Банк), SAN (DNS + IP)
    и математическую верификацию подписи по корневому сертификату.
    """
    engine = RootCAEngine(data_dir=tmp_path, passphrase="test-passphrase", key_size=2048)
    ca_cert, _ = engine.ensure_initialized()
    issuer = CertificateIssuer(engine)

    bank_cert, bank_key = issuer.issue_certificate(
        common_name="bank-simulation",
        role="bank_service",
        san_dns_names=["bank-simulation", "simulation-api.internal", "localhost"],
        san_ip_addresses=["127.0.0.1"],
        validity_days=365,
        key_size=2048,
        is_server=True,
        is_client=True,
    )

    assert bank_cert.serial_number == 2
    assert bank_cert.issuer == ca_cert.subject

    # Проверка расширения SAN
    san_ext = bank_cert.extensions.get_extension_for_oid(x509.ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
    dns_names = san_ext.get_values_for_type(x509.DNSName)
    assert "bank-simulation" in dns_names
    assert "simulation-api.internal" in dns_names

    # Математическая проверка подписи сертификата банка публичным ключом Root CA
    ca_cert.public_key().verify(
        bank_cert.signature,
        bank_cert.tbs_certificate_bytes,
        padding.PKCS1v15(),
        bank_cert.signature_hash_algorithm,
    )


def test_cert_issuer_client_mtls_certificate(tmp_path: Path):
    """Проверяет выпуск клиентского сертификата администратора для mTLS."""
    engine = RootCAEngine(data_dir=tmp_path, passphrase="test-passphrase", key_size=2048)
    ca_cert, _ = engine.ensure_initialized()
    issuer = CertificateIssuer(engine)

    client_cert, client_key = issuer.issue_certificate(
        common_name="superadmin-workstation",
        role="admin_operator",
        is_server=False,
        is_client=True,
        validity_days=365,
    )

    # Проверяем EKU: только clientAuth
    eku_ext = client_cert.extensions.get_extension_for_oid(x509.ExtensionOID.EXTENDED_KEY_USAGE).value
    assert x509.ExtendedKeyUsageOID.CLIENT_AUTH in eku_ext
    assert x509.ExtendedKeyUsageOID.SERVER_AUTH not in eku_ext

    # Математическая проверка подписи Root CA
    ca_cert.public_key().verify(
        client_cert.signature,
        client_cert.tbs_certificate_bytes,
        padding.PKCS1v15(),
        client_cert.signature_hash_algorithm,
    )


def test_bootstrap_pki_full_flow(tmp_path: Path):
    """
    Проверяет сквозной запуск Bootstrap PKI:
    генерацию корневого сертификата и сертификатов всех сервисов
    с формированием каталога MinIO.
    """
    data_dir = tmp_path / "ca_data"
    export_dir = tmp_path / "shared_certs"

    manifest = bootstrap_pki(
        data_dir=data_dir,
        export_dir=export_dir,
        passphrase="full-flow-passphrase-999",
        force=True,
    )

    assert (export_dir / "ca.crt").exists()
    assert (export_dir / "bank.crt").exists()
    assert (export_dir / "bank.key").exists()
    assert (export_dir / "admin_server.crt").exists()
    assert (export_dir / "admin_server.key").exists()
    assert (export_dir / "admin_client.crt").exists()
    assert (export_dir / "admin_client.key").exists()
    assert (export_dir / "postgres.crt").exists()
    assert (export_dir / "redis.crt").exists()
    assert (export_dir / "minio.crt").exists()

    # Проверяем MinIO специфичные пути
    assert (export_dir / "minio" / "public.crt").exists()
    assert (export_dir / "minio" / "private.key").exists()
    assert (export_dir / "minio" / "CAs" / "ca.crt").exists()

    # Проверяем обратную совместимость
    assert (export_dir / "server.crt").exists()
    assert (export_dir / "client.crt").exists()

    # Загружаем ca.crt и проверяем, что все сервисные сертификаты подписаны им
    root_cert = x509.load_pem_x509_certificate((export_dir / "ca.crt").read_bytes())
    for svc_name in ["bank", "admin_server", "admin_client", "postgres", "redis", "minio"]:
        svc_cert = x509.load_pem_x509_certificate((export_dir / f"{svc_name}.crt").read_bytes())
        root_cert.public_key().verify(
            svc_cert.signature,
            svc_cert.tbs_certificate_bytes,
            padding.PKCS1v15(),
            svc_cert.signature_hash_algorithm,
        )
