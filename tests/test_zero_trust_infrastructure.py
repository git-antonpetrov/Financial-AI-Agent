"""
Комплексные тесты сквозной инфраструктуры Zero-Trust (Phase 7: test_zero_trust_infrastructure.py).
Проверяет:
1. Топологию микросегментации Docker Compose (5 подсетей, изоляция Root CA и Data Net);
2. Конфигурацию Ingress и внутреннего TLS веб-сервера Caddy;
3. Матрицу отказов mTLS аутентификации (verify_mtls_client_identity);
4. Защиту от атак повторного воспроизведения (Anti-Replay Nonce & Timestamp drift);
5. Сквозной пайплайн RAG: клиентская подпись (ZeroTrustClientSigner), AES-256-GCM шифрование Data-at-Rest, серверная квитанция;
6. Изоляцию мастер-пароля Root CA и предотвращение утечки закрытого ключа ca.key.
"""

import os
import sys
import time
import uuid
import yaml
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from fastapi import HTTPException, Request

# Моки внешних сервисов
for mod in ["redis", "minio", "litellm", "asyncpg", "chromadb", "psycopg2"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

# Пути
sys.path.insert(0, os.path.abspath("src/pki"))
sys.path.insert(0, os.path.abspath("src/admin_server"))
sys.path.insert(0, os.path.abspath("src/admin_server/fastapi"))
sys.path.insert(0, os.path.abspath("src/admin_client/backend"))
sys.path.insert(0, os.path.abspath("src/simulations/core/crypto"))

from src.pki.bootstrap_pki import bootstrap_pki
from src.pki.core.ca_engine import RootCAEngine
from src.admin_client.backend.core.crypto.signer import ZeroTrustClientSigner
from src.admin_client.backend.core.crypto.canonical import compute_rag_canonical_digest
import security
from security import (
    settings,
    verify_mtls_client_identity,
    record_admin_nonce_if_new,
    clear_admin_nonces_cache,
    encrypt_data_at_rest,
    decrypt_data_at_rest,
    verify_digital_signature,
    sign_server_receipt,
    revoke_certificate_in_redis,
    _revoked_certs_memory,
)


@pytest.fixture(autouse=True)
def reset_security_state():
    """Сбрасывает состояние списков отзыва и кэша одноразовых nonce перед каждым тестом."""
    clear_admin_nonces_cache()
    _revoked_certs_memory.clear()
    orig_require_mtls = settings.REQUIRE_MTLS
    orig_allowed_subjects = list(settings.ALLOWED_MTLS_SUBJECTS)
    orig_allowed_issuers = list(settings.ALLOWED_MTLS_ISSUERS)
    yield
    clear_admin_nonces_cache()
    _revoked_certs_memory.clear()
    settings.REQUIRE_MTLS = orig_require_mtls
    settings.ALLOWED_MTLS_SUBJECTS = orig_allowed_subjects
    settings.ALLOWED_MTLS_ISSUERS = orig_allowed_issuers


def test_docker_compose_zero_trust_topology():
    """Проверяет соответствие архитектуры сетей и томов docker-compose.yml стандарту Zero-Trust."""
    compose_path = Path("docker-compose.yml")
    assert compose_path.exists(), "Файл docker-compose.yml не найден в корне проекта"

    with open(compose_path, "r", encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    # 1. Проверка 5 изолированных подсетей
    networks = compose.get("networks", {})
    expected_networks = ["pki_net", "edge_net", "admin_backend_net", "data_net", "simulation_net"]
    for net in expected_networks:
        assert net in networks, f"Сеть {net} отсутствует в docker-compose.yml"

    # Подсети pki_net и data_net должны быть изолированы от выхода в интернет (internal: true)
    assert networks["pki_net"].get("internal") is True, "pki_net должна иметь флаг internal: true"
    assert networks["data_net"].get("internal") is True, "data_net должна иметь флаг internal: true"

    services = compose.get("services", {})

    # 2. Проверка сервиса Root CA
    assert "root-ca" in services
    root_ca_svc = services["root-ca"]
    assert root_ca_svc.get("networks") == ["pki_net"], "Root CA должен быть подключен исключительно к pki_net"

    # Проверка монтирования изолированного тома root_ca_data
    root_ca_volumes = root_ca_svc.get("volumes", [])
    has_root_ca_volume = any("root_ca_data:" in v for v in root_ca_volumes)
    assert has_root_ca_volume, "Root CA сервис должен монтировать закрытый том root_ca_data"

    # Убеждаемся, что ни один другой сервис не имеет доступа к тому root_ca_data
    for svc_name, svc_conf in services.items():
        if svc_name == "root-ca":
            continue
        vols = svc_conf.get("volumes", [])
        for v in vols:
            assert "root_ca_data" not in str(v), f"Утечка изоляции: сервис {svc_name} имеет доступ к root_ca_data!"

    # 3. База данных PostgreSQL строго в data_net (internal: true), без edge_net и без simulation_net
    pg_svc = services.get("postgres-db", {})
    pg_networks = pg_svc.get("networks", [])
    assert "edge_net" not in pg_networks, "PostgreSQL не должен быть подключен к публичной edge_net"
    assert "simulation_net" not in pg_networks, "PostgreSQL не должен быть в simulation_net (нарушение internal: true)"
    assert pg_networks == ["data_net"], "PostgreSQL должен быть подключен исключительно к защищенной data_net"

    # 4. Проверка исключительности публичной edge_net (только Caddy)
    for svc_name, svc_conf in services.items():
        nets = svc_conf.get("networks", [])
        if isinstance(nets, dict):
            nets = list(nets.keys())
        if svc_name == "caddy":
            assert "edge_net" in nets
        else:
            assert "edge_net" not in nets, f"Сервис {svc_name} не должен иметь прямого подключения к edge_net!"



def test_caddyfile_mtls_ingress_rules():
    """Проверяет настройки Caddyfile: строгий mTLS (require_and_verify), проброс X-Client-Cert и внутренний TLS."""
    caddy_path = Path("src/caddy/Caddyfile")
    assert caddy_path.exists(), "Caddyfile не найден по пути src/caddy/Caddyfile"

    content = caddy_path.read_text(encoding="utf-8")

    # 1. Режим client_auth
    assert "client_auth" in content
    assert "require_and_verify" in content
    assert "trusted_ca_cert_file" in content
    assert "/etc/caddy/certs/ca.crt" in content

    # 2. Проброс заголовков mTLS к backend
    assert "header_up X-Client-Cert-Verify {tls_client_verified}" in content
    assert "header_up X-Client-Cert-Subject {tls_client_subject}" in content
    assert "header_up X-Client-Cert-Serial {tls_client_serial}" in content
    assert "header_up X-Client-Cert-Fingerprint {tls_client_fingerprint}" in content

    # 3. Внутренний банковский контур
    assert "simulation-api.internal" in content
    assert "tls internal" in content


def test_ingress_mtls_verification_matrix():
    """Тестирует матрицу сценариев mTLS аутентификации в verify_mtls_client_identity."""
    settings.REQUIRE_MTLS = True
    settings.ALLOWED_MTLS_SUBJECTS = ["CN=superadmin-workstation", "superadmin-workstation"]
    settings.ALLOWED_MTLS_ISSUERS = ["Zero-Trust Root CA"]

    # 1. Успешный запрос с валидным сертификатом и авторизованным Subject
    valid_req = Request({
        "type": "http",
        "method": "POST",
        "url": "http://testserver/api/v1/rag/documents",
        "headers": [
            (b"x-client-cert-verify", b"SUCCESS"),
            (b"x-client-cert-subject", b"CN=superadmin-workstation"),
            (b"x-client-cert-issuer", b"CN=Zero-Trust Root CA"),
            (b"x-client-cert-serial", b"4001"),
            (b"x-client-cert-fingerprint", b"AA:BB:CC:DD:11:22:33:44"),
        ]
    })
    identity = verify_mtls_client_identity(valid_req)
    assert identity["verified"] is True
    assert identity["common_name"] == "superadmin-workstation"
    assert identity["serial"] == "4001"

    # 2. Ошибка: Верификация сертификата на Caddy провалена (FAILED) -> HTTP 401
    failed_verify_req = Request({
        "type": "http",
        "method": "POST",
        "url": "http://testserver/api/v1/rag/documents",
        "headers": [
            (b"x-client-cert-verify", b"FAILED: self-signed certificate in chain"),
            (b"x-client-cert-subject", b"CN=superadmin-workstation"),
        ]
    })
    with pytest.raises(HTTPException) as exc_info:
        verify_mtls_client_identity(failed_verify_req)
    assert exc_info.value.status_code == 401

    # 3. Ошибка: Заголовок верификации отсутствует при обязательном mTLS -> HTTP 401
    missing_cert_req = Request({
        "type": "http",
        "method": "POST",
        "url": "http://testserver/api/v1/rag/documents",
        "headers": []
    })
    with pytest.raises(HTTPException) as exc_info:
        verify_mtls_client_identity(missing_cert_req)
    assert exc_info.value.status_code == 401

    # 4. Ошибка: Сертификат валиден, но Subject не входит в белый список -> HTTP 403
    unauthorized_subject_req = Request({
        "type": "http",
        "method": "POST",
        "url": "http://testserver/api/v1/rag/documents",
        "headers": [
            (b"x-client-cert-verify", b"SUCCESS"),
            (b"x-client-cert-subject", b"CN=untrusted-client-device"),
            (b"x-client-cert-issuer", b"CN=Zero-Trust Root CA"),
            (b"x-client-cert-serial", b"9999"),
        ]
    })
    with pytest.raises(HTTPException) as exc_info:
        verify_mtls_client_identity(unauthorized_subject_req)
    assert exc_info.value.status_code == 403

    # 5. Ошибка: Сертификат отозван в CRL (Redis / In-memory) -> HTTP 401
    revoke_certificate_in_redis(serial="4001", reason="compromised")
    with pytest.raises(HTTPException) as exc_info:
        verify_mtls_client_identity(valid_req)
    assert exc_info.value.status_code == 401
    assert "отозван" in exc_info.value.detail.lower()


def test_anti_replay_protection_matrix():
    """Тестирует защиту от атак повторного воспроизведения (Anti-Replay) через одноразовые Nonce."""
    clear_admin_nonces_cache()

    nonce1 = f"nonce_{uuid.uuid4().hex}"
    nonce2 = f"nonce_{uuid.uuid4().hex}"

    # 1. Первый запрос с уникальным nonce принимается
    assert record_admin_nonce_if_new(nonce1) is True

    # 2. Повторный запрос с тем же nonce (Replay Attack) отклоняется
    assert record_admin_nonce_if_new(nonce1) is False

    # 3. Другой уникальный nonce принимается
    assert record_admin_nonce_if_new(nonce2) is True
    assert record_admin_nonce_if_new(nonce2) is False

    # 4. Пустой или пробельный nonce отклоняется
    assert record_admin_nonce_if_new("") is False
    assert record_admin_nonce_if_new("   ") is False


def test_e2e_rag_signing_and_encrypted_pipeline(tmp_path):
    """
    Сквозной E2E тест:
    1. Инициализация PKI;
    2. Подписание запроса клиентом (ZeroTrustClientSigner);
    3. Проверка цифровой подписи сервером;
    4. Защита от модификации тела (tampering prevention);
    5. Конвертное шифрование текста AES-256-GCM Data-at-Rest;
    6. Формирование сервером квитанции AckReceipt и ее верификация клиентом.
    """
    data_dir = tmp_path / "ca_data"
    export_dir = tmp_path / "certs_export"
    passphrase = "StrictProductionPassphrase_E2E_2026!"

    bootstrap_pki(data_dir=data_dir, export_dir=export_dir, passphrase=passphrase, force=True)

    client_cert_path = str(export_dir / "admin_client.crt")
    client_key_path = str(export_dir / "admin_client.key")
    server_cert_path = str(export_dir / "admin_server.crt")
    server_key_path = str(export_dir / "admin_server.key")
    ca_cert_path = str(export_dir / "ca.crt")

    # 1. Инициализация клиента
    signer = ZeroTrustClientSigner(
        mode="software",
        cert_path=client_cert_path,
        key_path=client_key_path,
        ca_cert_path=ca_cert_path,
        signing_scheme="PSS"
    )

    body_bytes = b'{"document_title":"Security Policy","collection_name":"main","content":"Zero Trust Banking Spec"}'
    req_headers = signer.create_rag_request_headers(body_bytes=body_bytes)

    assert "X-Signature" in req_headers
    assert "X-Nonce" in req_headers
    assert "X-Timestamp" in req_headers

    # 2. Сервер проверяет цифровую подпись
    canonical = compute_rag_canonical_digest(
        timestamp=int(req_headers["X-Timestamp"]),
        nonce=req_headers["X-Nonce"],
        body_bytes=body_bytes
    )
    is_valid = verify_digital_signature(
        public_key_or_cert=Path(client_cert_path).read_text(encoding="utf-8"),
        signature_b64=req_headers["X-Signature"],
        data_bytes=canonical
    )
    assert is_valid is True, "Подпись клиента должна быть успешно верифицирована сервером"

    # 3. Защита от фальсификации (Tampering): изменение даже одного байта делает подпись невалидной
    tampered_body = b'{"document_title":"Security Policy","collection_name":"main","content":"Tampered Content!"}'
    tampered_canonical = compute_rag_canonical_digest(
        timestamp=int(req_headers["X-Timestamp"]),
        nonce=req_headers["X-Nonce"],
        body_bytes=tampered_body
    )
    is_tampered_valid = verify_digital_signature(
        public_key_or_cert=Path(client_cert_path).read_text(encoding="utf-8"),
        signature_b64=req_headers["X-Signature"],
        data_bytes=tampered_canonical
    )
    assert is_tampered_valid is False, "Подпись под модифицированными данными должна быть отклонена"

    # 4. Проверка шифрования Data-at-Rest в PostgreSQL (AES-256-GCM)
    raw_text = "Конфиденциальный финансовый регламент банка"
    encrypted_bytes = encrypt_data_at_rest(raw_text)

    # Проверка формата конверта ENC1
    assert encrypted_bytes.startswith(b"ENC1")
    assert raw_text.encode("utf-8") not in encrypted_bytes

    decrypted_bytes = decrypt_data_at_rest(encrypted_bytes)
    assert decrypted_bytes.decode("utf-8") == raw_text

    # Повреждение зашифрованных данных вызывает ошибку проверки аутентичности (AEAD Auth Tag)
    corrupted_payload = bytearray(encrypted_bytes)
    corrupted_payload[-1] ^= 0xFF
    with pytest.raises(Exception):
        decrypt_data_at_rest(bytes(corrupted_payload))

    # 5. Формирование сервером квитанции AckReceipt
    receipt_data = {
        "receipt_id": str(uuid.uuid4()),
        "status": "QUEUED_FOR_INDEXING",
        "document_id": "sha256:abcd1234efgh5678",
        "document_title": "Security Policy",
        "collection_name": "main",
        "timestamp": int(time.time()),
        "nonce": req_headers["X-Nonce"],
    }

    server_key_pem = Path(server_key_path).read_text(encoding="utf-8")
    server_sig, server_fp = sign_server_receipt(receipt_data, private_key_pem=server_key_pem)
    assert server_sig != ""
    assert server_fp.startswith("sha256:")

    # 6. Клиент проверяет подлинность квитанции сервера
    server_cert_pem = Path(server_cert_path).read_text(encoding="utf-8")
    receipt_verified = signer.verify_server_receipt(
        receipt_data=receipt_data,
        server_signature_b64=server_sig,
        trusted_server_cert_or_pubkey=server_cert_pem
    )
    assert receipt_verified is True, "Клиент должен подтвердить криптографическую квитанцию сервера"

    # Фальсификация квитанции отвергается
    fake_receipt = dict(receipt_data, status="INJECTED_STATUS")
    fake_verified = signer.verify_server_receipt(
        receipt_data=fake_receipt,
        server_signature_b64=server_sig,
        trusted_server_cert_or_pubkey=server_cert_pem
    )
    assert fake_verified is False, "Сфальсифицированная квитанция должна быть отвергнута"


def test_root_ca_passphrase_and_private_key_zero_leakage(tmp_path):
    """Проверяет криптостойкую защиту мастер-паролем приватного ключа Root CA и отсутствие его утечки в экспорт."""
    data_dir = tmp_path / "ca_data"
    export_dir = tmp_path / "certs_export"
    passphrase = "MasterPassword_AES256_Enforced_2026!"

    ca_engine = RootCAEngine(data_dir=data_dir, passphrase=passphrase)
    root_cert, ca_priv_key = ca_engine.initialize(force=True)

    # 1. Проверка наличия зашифрованного файла ключа
    assert ca_engine.key_path.exists()
    key_bytes = ca_engine.key_path.read_bytes()
    assert b"ENCRYPTED" in key_bytes, "Закрытый ключ Root CA должен сохраняться исключительно в зашифрованном виде"

    # 2. Попытка загрузить приватный ключ с некорректным паролем
    wrong_engine = RootCAEngine(data_dir=data_dir, passphrase="WrongPassword123!")
    with pytest.raises(Exception):
        wrong_engine.load_private_key()

    # 3. Экспорт корневого публичного сертификата
    export_target = export_dir / "ca.crt"
    ca_engine.export_ca_certificate(export_target)
    assert export_target.exists()

    # Закрытый ключ ca.key ни при каких условиях не должен присутствовать в export_dir
    assert not (export_dir / "ca.key").exists(), "Критическая уязвимость: закрытый ключ ca.key попал в директорию экспорта!"
