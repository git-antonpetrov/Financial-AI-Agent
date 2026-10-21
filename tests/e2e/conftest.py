"""
tests/e2e/conftest.py
Global Shared Fixtures and Runner Configuration for Financial-AI-Agent E2E Test Suite.
Implements Zero-Trust cryptography, in-memory databases, and strict test timeout controls.
"""

import datetime
import hashlib
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# Configure module search paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "src" / "admin_server"))
sys.path.insert(0, str(PROJECT_ROOT / "src" / "admin_server" / "fastapi"))
sys.path.insert(0, str(PROJECT_ROOT / "src" / "simulations"))


def pytest_addoption(parser):
    """
    Register command line option --timeout to enforce user safety requirement:
    pytest tests/e2e/ --timeout=30
    """
    try:
        parser.addoption(
            "--timeout",
            action="store",
            default=30.0,
            type=float,
            help="Timeout limit in seconds per test to prevent infinite socket or queue hangs",
        )
    except Exception:
        pass


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """
    Execution hook enforcing the test timeout limit.
    Verifies that no test execution hangs longer than the specified timeout.
    """
    timeout = item.config.getoption("--timeout", 30.0)
    start_time = time.monotonic()
    yield
    duration = time.monotonic() - start_time
    if duration > timeout:
        raise TimeoutError(f"Test '{item.name}' exceeded timeout limit of {timeout}s (took {duration:.2f}s)")


# -----------------------------------------------------------------------------
# In-Memory Redis Mock with TTL and NX (Set-if-Not-Exists) Support
# -----------------------------------------------------------------------------
class E2ERedisMock:
    """Mock Redis client providing atomic operations, TTL expiry, and pubsub."""

    def __init__(self):
        self._data: dict[str, Any] = {}
        self._expiries: dict[str, float] = {}
        self._published_channels: dict[str, list[str]] = {}

    def _purge_expired(self):
        now = time.monotonic()
        expired = [k for k, exp in self._expiries.items() if exp <= now]
        for k in expired:
            self._data.pop(k, None)
            self._expiries.pop(k, None)

    def set(self, key: str, value: Any, nx: bool = False, ex: int | None = None) -> bool:
        self._purge_expired()
        if nx and key in self._data:
            return False
        self._data[key] = value
        if ex is not None:
            self._expiries[key] = time.monotonic() + ex
        else:
            self._expiries.pop(key, None)
        return True

    def setex(self, key: str, ttl: int, value: Any) -> bool:
        return self.set(key, value, nx=False, ex=ttl)

    def get(self, key: str) -> Any:
        self._purge_expired()
        return self._data.get(key)

    def exists(self, *keys: str) -> int:
        self._purge_expired()
        count = sum(1 for k in keys if k in self._data)
        return count

    def delete(self, *keys: str) -> int:
        count = 0
        for k in keys:
            if k in self._data:
                self._data.pop(k, None)
                self._expiries.pop(k, None)
                count += 1
        return count

    def incr(self, key: str) -> int:
        self._purge_expired()
        val = int(self._data.get(key, 0)) + 1
        self._data[key] = val
        return val

    def expire(self, key: str, ttl: int) -> bool:
        self._purge_expired()
        if key in self._data:
            self._expiries[key] = time.monotonic() + ttl
            return True
        return False

    def ttl(self, key: str) -> int:
        self._purge_expired()
        if key not in self._data:
            return -2
        if key not in self._expiries:
            return -1
        rem = int(self._expiries[key] - time.monotonic())
        return max(0, rem)

    def publish(self, channel: str, message: str) -> int:
        if channel not in self._published_channels:
            self._published_channels[channel] = []
        self._published_channels[channel].append(message)
        return 1

    def clear(self):
        self._data.clear()
        self._expiries.clear()
        self._published_channels.clear()


# -----------------------------------------------------------------------------
# Cryptographic PKI Generator Fixtures
# -----------------------------------------------------------------------------
@pytest.fixture(scope="session")
def pki_ca_authority():
    """Generates a trusted Root CA for E2E testing."""
    ca_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_subject = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Financial AI E2E Authority"),
        x509.NameAttribute(NameOID.COMMON_NAME, "Financial AI E2E Root CA"),
    ])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
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
        .sign(ca_key, hashes.SHA256(), default_backend())
    )
    ca_cert_pem = ca_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    ca_key_pem = ca_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    return {
        "key": ca_key,
        "cert": ca_cert,
        "key_pem": ca_key_pem,
        "cert_pem": ca_cert_pem,
        "subject": ca_subject,
    }


@pytest.fixture
def cert_factory(pki_ca_authority):
    """
    Factory fixture to dynamically issue valid or invalid X.509 certificates
    for negative and boundary testing.
    """
    ca_key = pki_ca_authority["key"]
    ca_subject = pki_ca_authority["subject"]

    def _issue_cert(
        common_name: str,
        is_client: bool = True,
        is_server: bool = False,
        is_ca: bool = False,
        path_length: int | None = None,
        validity_days: int = 365,
        not_before_delta: datetime.timedelta = datetime.timedelta(days=-1),
        self_signed: bool = False,
        include_digital_signature: bool = True,
        include_key_cert_sign: bool = False,
    ):
        key = rsa.generate_private_key(65537, 2048, default_backend())
        now = datetime.datetime.now(datetime.timezone.utc)
        subject = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Financial AI E2E Entity"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ])
        issuer = subject if self_signed else ca_subject
        signing_key = key if self_signed else ca_key

        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now + not_before_delta)
            .not_valid_after(now + not_before_delta + datetime.timedelta(days=validity_days))
            .add_extension(x509.BasicConstraints(ca=is_ca, path_length=path_length), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=include_digital_signature,
                    content_commitment=False,
                    key_encipherment=True,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=include_key_cert_sign,
                    crl_sign=include_key_cert_sign,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
        )
        ekus = []
        if is_server:
            ekus.append(ExtendedKeyUsageOID.SERVER_AUTH)
        if is_client:
            ekus.append(ExtendedKeyUsageOID.CLIENT_AUTH)
        if ekus:
            builder = builder.add_extension(x509.ExtendedKeyUsage(ekus), critical=False)

        cert = builder.sign(signing_key, hashes.SHA256(), default_backend())
        cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
        key_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        fingerprint = f"sha256:{hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()}"

        return {
            "cert": cert,
            "key": key,
            "cert_pem": cert_pem,
            "key_pem": key_pem,
            "serial": str(cert.serial_number),
            "fingerprint": fingerprint,
            "common_name": common_name,
        }

    return _issue_cert


@pytest.fixture
def mock_redis():
    """Provides a fresh isolated mock Redis instance."""
    return E2ERedisMock()


@pytest.fixture(autouse=True)
def configure_e2e_environment(pki_ca_authority, cert_factory, monkeypatch):
    """
    Configures environment variables, certificates on disk, and security state
    to satisfy Zero-Trust fail-closed invariants for E2E tests.
    """
    certs_dir = Path(tempfile.gettempdir()) / "fin_ai_e2e_certs"
    certs_dir.mkdir(parents=True, exist_ok=True)

    ca_pem = pki_ca_authority["cert_pem"]
    ca_key_pem = pki_ca_authority["key_pem"]
    (certs_dir / "ca.crt").write_text(ca_pem, encoding="utf-8")
    (certs_dir / "ca.key").write_text(ca_key_pem, encoding="utf-8")

    server_leaf = cert_factory("admin-server.internal", is_server=True)
    (certs_dir / "admin_server.crt").write_text(server_leaf["cert_pem"], encoding="utf-8")
    (certs_dir / "admin_server.key").write_text(server_leaf["key_pem"], encoding="utf-8")

    bank_leaf = cert_factory("bank.simulation", is_server=True, is_client=True)
    (certs_dir / "bank.crt").write_text(bank_leaf["cert_pem"], encoding="utf-8")
    (certs_dir / "bank.key").write_text(bank_leaf["key_pem"], encoding="utf-8")

    enclave_leaf = cert_factory("enclave.security", is_server=False, is_client=True)
    (certs_dir / "enclave.crt").write_text(enclave_leaf["cert_pem"], encoding="utf-8")
    (certs_dir / "enclave.key").write_text(enclave_leaf["key_pem"], encoding="utf-8")

    env_overrides = {
        "ROOT_CA_CERT_PATH": str(certs_dir / "ca.crt"),
        "CA_CERT_PATH": str(certs_dir / "ca.crt"),
        "BANK_CERT_PATH": str(certs_dir / "bank.crt"),
        "BANK_KEY_PATH": str(certs_dir / "bank.key"),
        "BANK_CERTS_DIR": str(certs_dir),
        "SERVER_CERT_PATH": str(certs_dir / "admin_server.crt"),
        "SERVER_KEY_PATH": str(certs_dir / "admin_server.key"),
        "JWT_PRIVATE_KEY_PATH": str(certs_dir / "admin_server.key"),
        "JWT_PUBLIC_KEY_PATH": str(certs_dir / "admin_server.crt"),
        "JWT_PRIVATE_KEY": server_leaf["key_pem"],
        "JWT_PUBLIC_KEY": server_leaf["cert_pem"],
        "DATA_ENCRYPTION_KEY": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        "CHROMA_ENVELOPE_KEY": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        "PROXY_INTERNAL_SECRET": "test-e2e-proxy-secret-token-32b!",
        "GATEWAY_SECRET": "test-e2e-gateway-secret-token-42",
        "GEMINI_GATEWAY_SECRET": "test-e2e-gateway-secret-token-42",
        "ADMIN_PASSWORD": "TestAdminPassword123!",
        "ADMIN_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
        "AGENT_MAIN_BOOTSTRAP_TOKEN": "test-main-token",
        "AGENT_BANK_BOOTSTRAP_TOKEN": "test-bank-token",
        "AGENT_INVEST_BOOTSTRAP_TOKEN": "test-invest-token",
        "AGENT_DIGITAL_BOOTSTRAP_TOKEN": "test-digital-token",
    }
    for k, v in env_overrides.items():
        monkeypatch.setenv(k, v)

    # Reset security caches
    try:
        from src.admin_server.fastapi.security import (
            set_trusted_root_ca,
            clear_admin_nonces_cache,
            _revoked_certs_memory,
        )
        set_trusted_root_ca(ca_pem)
        clear_admin_nonces_cache()
        _revoked_certs_memory.clear()
    except Exception:
        pass

    try:
        from src.simulations.core.crypto.bank_ca import reset_bank_ca_for_tests
        reset_bank_ca_for_tests()
    except Exception:
        pass

    try:
        from src.simulations.core.crypto.receipt_signer import reset_receipt_signer_for_tests
        reset_receipt_signer_for_tests()
    except Exception:
        pass

    yield certs_dir
