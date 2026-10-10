"""
tests/conftest.py

Глобальная конфигурация Pytest для тестов Financial-AI-Agent.
Настраивает тестовые криптографические ключи, сертификаты на диске и параметры окружения
для соответствия модели Zero-Trust без использования небезопасных in-memory фолбэков в проде.
"""

import os
import sys
import tempfile
import datetime
import pytest
from pathlib import Path
from cryptography import x509
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# 1. Генерация доверенного Root CA
_ca_key = rsa.generate_private_key(
    public_exponent=65537,
    key_size=2048,
    backend=default_backend(),
)
_now = datetime.datetime.now(datetime.timezone.utc)
_ca_subject = x509.Name([
    x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
    x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Financial AI Agent Core"),
    x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Root Security Authority"),
    x509.NameAttribute(NameOID.COMMON_NAME, "Financial AI Agent Root CA"),
])
_ca_cert = (
    x509.CertificateBuilder()
    .subject_name(_ca_subject)
    .issuer_name(_ca_subject)
    .public_key(_ca_key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(_now - datetime.timedelta(days=1))
    .not_valid_after(_now + datetime.timedelta(days=3650))
    .add_extension(x509.BasicConstraints(ca=True, path_length=2), critical=True)
    .add_extension(
        x509.KeyUsage(
            digital_signature=True,
            content_commitment=False,
            key_encipherment=False,
            data_encipherment=False,
            key_agreement=False,
            key_cert_sign=True,
            crl_sign=True,
            encipher_only=False,
            decipher_only=False,
        ),
        critical=True,
    )
    .sign(_ca_key, hashes.SHA256(), default_backend())
)

_ca_cert_pem = _ca_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
_ca_key_pem = _ca_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("utf-8")

# 2. Выпуск сертификата Банка (подписан Root CA)
_bank_key = rsa.generate_private_key(65537, 2048, default_backend())
_bank_subject = x509.Name([
    x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
    x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Financial AI Simulation Bank"),
    x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Bank Core Operations"),
    x509.NameAttribute(NameOID.COMMON_NAME, "bank.simulation"),
])
_bank_cert = (
    x509.CertificateBuilder()
    .subject_name(_bank_subject)
    .issuer_name(_ca_subject)
    .public_key(_bank_key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(_now - datetime.timedelta(days=1))
    .not_valid_after(_now + datetime.timedelta(days=730))
    .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
    .add_extension(
        x509.KeyUsage(
            digital_signature=True,
            content_commitment=True,
            key_encipherment=True,
            data_encipherment=False,
            key_agreement=False,
            key_cert_sign=False,
            crl_sign=False,
            encipher_only=False,
            decipher_only=False,
        ),
        critical=True,
    )
    .sign(_ca_key, hashes.SHA256(), default_backend())
)
_bank_cert_pem = _bank_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
_bank_key_pem = _bank_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("utf-8")

# 3. Выпуск сертификата Admin Server (подписан Root CA)
_server_key = rsa.generate_private_key(65537, 2048, default_backend())
_server_subject = x509.Name([
    x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
    x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Financial AI Test"),
    x509.NameAttribute(NameOID.COMMON_NAME, "admin-server.test"),
])
_server_cert = (
    x509.CertificateBuilder()
    .subject_name(_server_subject)
    .issuer_name(_ca_subject)
    .public_key(_server_key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(_now - datetime.timedelta(days=1))
    .not_valid_after(_now + datetime.timedelta(days=365))
    .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
    .sign(_ca_key, hashes.SHA256(), default_backend())
)
_server_cert_pem = _server_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
_server_key_pem = _server_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("utf-8")
_server_pub_pem = _server_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode("utf-8")

# 4. Сохранение сертификатов на диск для строгого Zero-Trust режима (требующего файлы)
_certs_dir = Path(tempfile.gettempdir()) / "fin_ai_test_pki_certs"
_certs_dir.mkdir(parents=True, exist_ok=True)
(_certs_dir / "ca.crt").write_text(_ca_cert_pem, encoding="utf-8")
(_certs_dir / "ca.key").write_text(_ca_key_pem, encoding="utf-8")
(_certs_dir / "bank.crt").write_text(_bank_cert_pem, encoding="utf-8")
(_certs_dir / "bank.key").write_text(_bank_key_pem, encoding="utf-8")
(_certs_dir / "admin_server.crt").write_text(_server_cert_pem, encoding="utf-8")
(_certs_dir / "admin_server.key").write_text(_server_key_pem, encoding="utf-8")

_DEFAULT_ENV = {
    "ROOT_CA_CERT_PATH": str(_certs_dir / "ca.crt"),
    "BANK_CERT_PATH": str(_certs_dir / "bank.crt"),
    "BANK_KEY_PATH": str(_certs_dir / "bank.key"),
    "BANK_CERTS_DIR": str(_certs_dir),
    "JWT_PRIVATE_KEY_PATH": str(_certs_dir / "admin_server.key"),
    "JWT_PUBLIC_KEY_PATH": str(_certs_dir / "admin_server.crt"),
    "SERVER_CERT_PATH": str(_certs_dir / "admin_server.crt"),
    "SERVER_KEY_PATH": str(_certs_dir / "admin_server.key"),
    "JWT_PRIVATE_KEY": _server_key_pem,
    "JWT_PUBLIC_KEY": _server_pub_pem,
    "SERVER_CERT": _server_cert_pem,
    "DATA_ENCRYPTION_KEY": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "ROOT_CA_PASSPHRASE": "test-secret-ca-passphrase-4096-long",
    "AGENT_DIGITAL_BOOTSTRAP_TOKEN": "test-bootstrap-digital-token-12345",
    "AGENT_BANK_BOOTSTRAP_TOKEN": "test-bootstrap-bank-token-12345",
    "AGENT_INVEST_BOOTSTRAP_TOKEN": "test-bootstrap-invest-token-12345",
    "AGENT_MAIN_BOOTSTRAP_TOKEN": "test-bootstrap-main-token-12345",
    "ADMIN_PASSWORD": "TestAdminPassword123!",
    "ADMIN_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
    "POSTGRES_PASSWORD": "test_postgres_secret",
}

# Экспортируем в os.environ сразу при импорте conftest (до импорта любого теста)
for k, v in _DEFAULT_ENV.items():
    if k not in os.environ:
        os.environ[k] = v


def pytest_configure(config):
    """Хук конфигурации pytest: гарантирует наличие всех необходимых переменных окружения."""
    for k, v in _DEFAULT_ENV.items():
        if k not in os.environ:
            os.environ[k] = v


@pytest.fixture(scope="session", autouse=True)
def setup_test_crypto_environment_session():
    """
    Сессионная фикстура pytest для установки валидных тестовых ключей окружения
    (JWT_PRIVATE_KEY, DATA_ENCRYPTION_KEY, ROOT_CA_PASSPHRASE), чтобы все тесты
    проходили в strict production режиме.
    """
    for k, v in _DEFAULT_ENV.items():
        os.environ[k] = v
    yield


@pytest.fixture(autouse=True)
def setup_test_crypto_environment():
    """Автоматическая фикстура для поддержания базовых переменных окружения и изоляции тестов."""
    try:
        from src.simulations.core.crypto.bank_ca import reset_bank_ca_for_tests
        reset_bank_ca_for_tests()
    except Exception:
        pass
    try:
        from src.admin_server.fastapi.security import set_trusted_root_ca, clear_admin_nonces_cache
        set_trusted_root_ca(_ca_cert_pem)
        clear_admin_nonces_cache()
    except Exception:
        pass
    yield

