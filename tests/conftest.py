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
import fastapi  # noqa: F401  # Гарантируем предзагрузку реального пакета FastAPI из site-packages
import pytest
from pathlib import Path
from cryptography import x509
from cryptography.x509.oid import NameOID
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

# 4. Выпуск тестовых ключей Анклава и Оракула (SEC-13)
_enclave_key = rsa.generate_private_key(65537, 2048, default_backend())
_enclave_key_pem = _enclave_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("utf-8")
_enclave_pub_pem = _enclave_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode("utf-8")

_oracle_key = rsa.generate_private_key(65537, 2048, default_backend())
_oracle_key_pem = _oracle_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("utf-8")
_oracle_pub_pem = _oracle_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode("utf-8")

# 5. Сохранение сертификатов и ключей на диск для строгого Zero-Trust режима (требующего файлы)
_certs_dir = Path(tempfile.gettempdir()) / "fin_ai_test_pki_certs"
_certs_dir.mkdir(parents=True, exist_ok=True)
(_certs_dir / "ca.crt").write_text(_ca_cert_pem, encoding="utf-8")
(_certs_dir / "ca.key").write_text(_ca_key_pem, encoding="utf-8")
(_certs_dir / "bank.crt").write_text(_bank_cert_pem, encoding="utf-8")
(_certs_dir / "bank.key").write_text(_bank_key_pem, encoding="utf-8")
(_certs_dir / "admin_server.crt").write_text(_server_cert_pem, encoding="utf-8")
(_certs_dir / "admin_server.key").write_text(_server_key_pem, encoding="utf-8")
(_certs_dir / "enclave.pub").write_text(_enclave_pub_pem, encoding="utf-8")
(_certs_dir / "enclave.key").write_text(_enclave_key_pem, encoding="utf-8")
(_certs_dir / "oracle.pub").write_text(_oracle_pub_pem, encoding="utf-8")
(_certs_dir / "oracle.key").write_text(_oracle_key_pem, encoding="utf-8")

_DEFAULT_ENV = {
    "ROOT_CA_CERT_PATH": str(_certs_dir / "ca.crt"),
    "BANK_CERT_PATH": str(_certs_dir / "bank.crt"),
    "BANK_KEY_PATH": str(_certs_dir / "bank.key"),
    "BANK_CERTS_DIR": str(_certs_dir),
    "JWT_PRIVATE_KEY_PATH": str(_certs_dir / "admin_server.key"),
    "JWT_PUBLIC_KEY_PATH": str(_certs_dir / "admin_server.crt"),
    "SERVER_CERT_PATH": str(_certs_dir / "admin_server.crt"),
    "SERVER_KEY_PATH": str(_certs_dir / "admin_server.key"),
    "ENCLAVE_PUBLIC_KEY_PATH": str(_certs_dir / "enclave.pub"),
    "ENCLAVE_PRIVATE_KEY_PATH": str(_certs_dir / "enclave.key"),
    "ENCLAVE_PUBLIC_KEY_PEM": _enclave_pub_pem,
    "ENCLAVE_PRIVATE_KEY_PEM": _enclave_key_pem,
    "ORACLE_PUBLIC_KEY_PATH": str(_certs_dir / "oracle.pub"),
    "ORACLE_PRIVATE_KEY_PATH": str(_certs_dir / "oracle.key"),
    "ORACLE_PUBLIC_KEY_PEM": _oracle_pub_pem,
    "ORACLE_PRIVATE_KEY_PEM": _oracle_key_pem,
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
    "FINANCIAL_AGENT_CERTS_CACHE_DIR": str(_certs_dir),
    "BANK_DATABASE_URL": "postgresql+asyncpg://financial_ai_agent:test_postgres_secret@postgres-db:5432/bank_db",
    "INVEST_DATABASE_URL": "postgresql+asyncpg://financial_ai_agent:test_postgres_secret@postgres-db:5432/invest_db",
    "RUBLE_DATABASE_URL": "postgresql+asyncpg://financial_ai_agent:test_postgres_secret@postgres-db:5432/ruble_db",
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
        (_certs_dir / "ca.crt").write_text(_ca_cert_pem, encoding="utf-8")
        (_certs_dir / "ca.key").write_text(_ca_key_pem, encoding="utf-8")
        (_certs_dir / "bank.crt").write_text(_bank_cert_pem, encoding="utf-8")
        (_certs_dir / "bank.key").write_text(_bank_key_pem, encoding="utf-8")
        (_certs_dir / "admin_server.crt").write_text(_server_cert_pem, encoding="utf-8")
        (_certs_dir / "admin_server.key").write_text(_server_key_pem, encoding="utf-8")
        (_certs_dir / "enclave.pub").write_text(_enclave_pub_pem, encoding="utf-8")
        (_certs_dir / "enclave.key").write_text(_enclave_key_pem, encoding="utf-8")
        (_certs_dir / "oracle.pub").write_text(_oracle_pub_pem, encoding="utf-8")
        (_certs_dir / "oracle.key").write_text(_oracle_key_pem, encoding="utf-8")
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
    try:
        from src.simulations.core.crypto.chain_validator import reset_chain_validator_for_tests
        reset_chain_validator_for_tests()
    except Exception:
        pass
    try:
        import sys
        if "security" in sys.modules:
            sys.modules["security"].set_trusted_root_ca(_ca_cert_pem)
            sys.modules["security"].clear_admin_nonces_cache()
        from src.admin_server.fastapi.security import set_trusted_root_ca, clear_admin_nonces_cache
        set_trusted_root_ca(_ca_cert_pem)
        clear_admin_nonces_cache()
    except Exception:
        pass
    try:
        from src.simulations.core.crypto.enclave_verifier import reset_enclave_verifier_for_tests
        reset_enclave_verifier_for_tests()
    except Exception:
        pass
    try:
        from src.simulations.core.crypto.oracle_verifier import reset_oracle_verifier_for_tests
        reset_oracle_verifier_for_tests()
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def setup_test_fastapi_db():
    """Обеспечивает валидную асинхронную сессию БД для тестов эндпоинтов FastAPI."""
    from pathlib import Path
    from unittest.mock import MagicMock
    repo_root = Path(__file__).resolve().parent.parent
    p1 = str(repo_root / "src" / "admin_server")
    p2 = str(repo_root / "src" / "admin_server" / "fastapi")
    if p1 not in sys.path:
        sys.path.append(p1)
    if p2 not in sys.path:
        sys.path.insert(0, p2)

    apps = []
    overridden_deps = []
    try:
        for mod in [
            "redis",
            "minio",
            "litellm",
            "asyncpg",
            "psycopg2",
            "psycopg2.pool",
            "chromadb",
            "chromadb.config",
            "langchain_text_splitters",
        ]:
            if mod not in sys.modules:
                sys.modules[mod] = MagicMock()

        try:
            import server
            if hasattr(server, "app") and server.app not in apps:
                apps.append(server.app)
        except Exception:
            pass
        try:
            from src.admin_server.fastapi import server as srv_pkg
            if hasattr(srv_pkg, "app") and srv_pkg.app not in apps:
                apps.append(srv_pkg.app)
        except Exception:
            pass

        all_get_dbs = set()
        for mod_name in [
            "db.database",
            "src.admin_server.fastapi.db.database",
            "server",
            "src.admin_server.fastapi.server",
            "routers.rag_documents",
            "src.admin_server.fastapi.routers.rag_documents",
        ]:
            try:
                mod = sys.modules.get(mod_name)
                if mod is None:
                    __import__(mod_name)
                    mod = sys.modules.get(mod_name)
                if mod and hasattr(mod, "get_db"):
                    all_get_dbs.add(getattr(mod, "get_db"))
            except Exception:
                pass

        class InMemoryTestSession:
            def __init__(self):
                self.logs = []

            def get_bind(self):
                return None

            def add(self, obj):
                if not hasattr(obj, "id") or obj.id is None:
                    obj.id = len(self.logs) + 1
                self.logs.append(obj)

            async def commit(self):
                pass

            async def refresh(self, obj):
                pass

            async def rollback(self):
                pass

            async def delete(self, obj):
                if obj in self.logs:
                    self.logs.remove(obj)

            async def execute(self, q, *args, **kwargs):
                m = MagicMock()
                m.scalar_one_or_none.return_value = None
                m.scalars.return_value.first.return_value = self.logs[-1] if self.logs else None
                m.scalars.return_value.all.return_value = self.logs
                return m

        test_sess = InMemoryTestSession()

        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock

        @asynccontextmanager
        async def _mock_engine_begin(*args, **kwargs):
            mock_conn = AsyncMock()
            mock_conn.run_sync = AsyncMock()
            yield mock_conn

        @asynccontextmanager
        async def _mock_async_session(*args, **kwargs):
            yield test_sess

        async def _mock_revocation_listener(*args, **kwargs):
            return

        for mod_name in [
            "server",
            "src.admin_server.fastapi.server",
            "db.database",
            "src.admin_server.fastapi.db.database",
            "src.simulations.api.main",
            "src.simulations.api.core.auth",
        ]:
            mod = sys.modules.get(mod_name)
            if mod:
                if hasattr(mod, "engine"):
                    mod.engine.begin = _mock_engine_begin
                if hasattr(mod, "async_session"):
                    setattr(mod, "async_session", _mock_async_session)
                if hasattr(mod, "run_revocation_listener"):
                    setattr(mod, "run_revocation_listener", _mock_revocation_listener)
                if hasattr(mod, "run_simulation_revocation_listener"):
                    setattr(mod, "run_simulation_revocation_listener", _mock_revocation_listener)
                if hasattr(mod, "minio_client") and not isinstance(getattr(mod, "minio_client"), MagicMock):
                    mock_minio = MagicMock()
                    mock_minio.bucket_exists.return_value = True
                    setattr(mod, "minio_client", mock_minio)

        def _override_routes(target_app, routes):
            for route in routes:
                if hasattr(route, "dependant") and hasattr(route.dependant, "dependencies"):
                    for dep in route.dependant.dependencies:
                        if getattr(dep.call, "__name__", "") == "get_db":
                            target_app.dependency_overrides[dep.call] = lambda: test_sess
                            overridden_deps.append((target_app, dep.call))
                            all_get_dbs.add(dep.call)
                if hasattr(route, "routes"):
                    _override_routes(target_app, route.routes)

        for target_app in apps:
            _override_routes(target_app, target_app.routes)
            for gdb in all_get_dbs:
                target_app.dependency_overrides[gdb] = lambda: test_sess
                overridden_deps.append((target_app, gdb))

    except Exception:
        pass
    yield
    try:
        for target_app, dep in overridden_deps:
            target_app.dependency_overrides.pop(dep, None)
    except Exception:
        pass

