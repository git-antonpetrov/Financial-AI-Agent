import os
import sys
import re
from pathlib import Path
from unittest.mock import MagicMock
import pytest
import yaml

# Предварительный импорт fastapi до модификаций sys.path
import fastapi
from fastapi import Request, HTTPException
from fastapi.testclient import TestClient

# Моки внешних сервисов
for mod in ["redis", "minio", "litellm", "asyncpg"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

# Настройка путей
sys.path.insert(0, os.path.abspath("src/admin_server"))
sys.path.insert(0, os.path.abspath("src/admin_server/fastapi"))

import security
from security import (
    settings,
    extract_cn_from_subject,
    parse_mtls_client_certificate,
    verify_mtls_client_certificate,
)
import server
from server import app


# ------------------------------------------------------------------------------
# 1. Тесты микросегментации сетей Docker Compose
# ------------------------------------------------------------------------------

def test_docker_compose_network_microsegmentation():
    """
    Проверяет, что Docker Compose реализует 4 сегментированные сети:
    edge_net, backend_net, data_net (с флагом internal: true), simulation_net.
    Проверяет изоляцию Caddy, БД и сервисов симуляций.
    """
    compose_path = Path(__file__).parent.parent / "docker-compose.yml"
    assert compose_path.exists(), "docker-compose.yml не найден"

    with open(compose_path, "r", encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    assert "networks" in compose, "В docker-compose.yml должна быть секция 'networks'"
    networks = compose["networks"]

    # 1. Проверяем наличие всех 4 сетей
    assert "edge_net" in networks, "Сеть edge_net отсутствует"
    assert "backend_net" in networks, "Сеть backend_net отсутствует"
    assert "data_net" in networks, "Сеть data_net отсутствует"
    assert "simulation_net" in networks, "Сеть simulation_net отсутствует"

    # 2. Сеть data_net должна иметь internal: true для изоляции от внешнего интернета
    data_net_config = networks["data_net"]
    assert data_net_config.get("internal") is True, "Сеть data_net должна быть помечена как 'internal: true'"

    services = compose["services"]

    # 3. Caddy подключен ТОЛЬКО к edge_net
    assert "caddy" in services
    assert services["caddy"].get("networks") == ["edge_net"], "Caddy должен быть подключен только к edge_net"

    # 4. Воркеры симуляций изолированы: подключены к simulation_net и не имеют доступа к redis/minio/chroma
    simulation_services = ["simulation-api", "bank-worker", "invest-worker", "digital-worker", "simulations-seeder"]
    for s_name in simulation_services:
        assert s_name in services, f"Сервис {s_name} отсутствует в docker-compose.yml"
        s_nets = services[s_name].get("networks", [])
        assert "simulation_net" in s_nets, f"{s_name} должен быть в simulation_net"
        assert "backend_net" not in s_nets, f"{s_name} не должен иметь доступа к backend_net"
        assert "edge_net" not in s_nets, f"{s_name} не должен иметь доступа к edge_net"

    # 5. Хранилища minio и chromadb подключены к data_net и изолированы от simulation_net
    assert services["minio"].get("networks") == ["data_net"]
    assert services["chromadb"].get("networks") == ["data_net"]

    # 6. Redis подключен к backend_net и изолирован от simulation_net
    assert services["redis"].get("networks") == ["backend_net"]

    # 7. Admin Server объединяет edge_net, backend_net и data_net
    admin_nets = set(services["admin-server"].get("networks", []))
    assert admin_nets == {"edge_net", "backend_net", "data_net"}


# ------------------------------------------------------------------------------
# 2. Тесты конфигурации Caddy (TLS 1.3 / mTLS / Headers)
# ------------------------------------------------------------------------------

def test_caddyfile_tls_and_mtls_configuration():
    """
    Проверяет настройки безопасности в src/caddy/Caddyfile:
    протоколы TLS 1.3/1.2, стойкие шифры, client_auth и проксирование mTLS-заголовков.
    """
    caddyfile_path = Path(__file__).parent.parent / "src" / "caddy" / "Caddyfile"
    assert caddyfile_path.exists(), "src/caddy/Caddyfile не найден"

    content = caddyfile_path.read_text(encoding="utf-8")

    # Проверка строгих версий TLS
    assert ("protocols tls1.2 tls1.3" in content or "protocols tls1.3 tls1.2" in content), "Caddyfile должен требовать протоколы tls1.2 и tls1.3"
    assert "TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256" in content, "Caddyfile должен содержать безопасные TLS шифры"

    # Проверка client_auth
    assert "client_auth" in content, "Caddyfile должен содержать блок client_auth"
    assert "trusted_ca_cert_file" in content, "Caddyfile должен определять доверенный trusted_ca_cert_file"

    # Проверка проброса mTLS-заголовков
    expected_headers = [
        "X-Client-Cert-Verify",
        "X-Client-Cert-Subject",
        "X-Client-Cert-Issuer",
        "X-Client-Cert-Serial",
        "X-Client-Cert-Fingerprint",
    ]
    for h in expected_headers:
        assert h in content, f"Caddyfile должен передавать заголовок {h}"


# ------------------------------------------------------------------------------
# 3. Модульные тесты парсинга и валидации mTLS в FastAPI
# ------------------------------------------------------------------------------

def test_extract_cn_from_subject():
    """Проверяет корректность извлечения Common Name (CN) из Subject сертификата."""
    assert extract_cn_from_subject("CN=admin-client,O=FinancialAI,C=RU") == "admin-client"
    assert extract_cn_from_subject("/C=RU/O=FinancialAI/CN=desktop-operator") == "desktop-operator"
    assert extract_cn_from_subject("CN=user.name@example.com") == "user.name@example.com"
    assert extract_cn_from_subject("O=FinancialAI,OU=Dev") is None
    assert extract_cn_from_subject(None) is None


def test_parse_mtls_client_certificate():
    """Проверяет разбор mTLS-заголовков из запроса."""
    scope = {
        "type": "http",
        "headers": [
            (b"x-client-cert-verify", b"SUCCESS"),
            (b"x-client-cert-subject", b"CN=admin-client,O=FinancialAI,C=RU"),
            (b"x-client-cert-issuer", b"CN=FinancialAI Internal Root CA,O=FinancialAI"),
            (b"x-client-cert-serial", b"1001"),
            (b"x-client-cert-fingerprint", b"AABBCCDDEEFF"),
        ]
    }
    request = Request(scope)
    cert_info = parse_mtls_client_certificate(request)

    assert cert_info["verified"] is True
    assert cert_info["common_name"] == "admin-client"
    assert cert_info["subject"] == "CN=admin-client,O=FinancialAI,C=RU"
    assert cert_info["issuer"] == "CN=FinancialAI Internal Root CA,O=FinancialAI"
    assert cert_info["serial"] == "1001"
    assert cert_info["fingerprint"] == "AABBCCDDEEFF"
    assert cert_info["is_present"] is True


def test_parse_mtls_client_certificate_unverified_or_missing():
    """Проверяет разбор отсутствующего или непрошедшего валидацию сертификата."""
    scope = {
        "type": "http",
        "headers": [
            (b"x-client-cert-verify", b"FAILED:certificate expired"),
        ]
    }
    request = Request(scope)
    cert_info = parse_mtls_client_certificate(request)

    assert cert_info["verified"] is False
    assert cert_info["raw_verify_status"] == "FAILED:certificate expired"
    assert cert_info["common_name"] is None


def test_verify_mtls_permissive_mode(monkeypatch):
    """
    В режиме REQUIRE_MTLS=False валидация не блокирует запросы даже без сертификата.
    """
    monkeypatch.setattr(settings, "REQUIRE_MTLS", False)
    scope = {"type": "http", "headers": []}
    request = Request(scope)

    cert_info = verify_mtls_client_certificate(request)
    assert cert_info["verified"] is False
    assert cert_info["is_present"] is False


def test_verify_mtls_strict_mode_blocks_unverified(monkeypatch):
    """
    В режиме REQUIRE_MTLS=True запросы без валидного сертификата отклоняются (401).
    """
    monkeypatch.setattr(settings, "REQUIRE_MTLS", True)
    scope = {"type": "http", "headers": []}
    request = Request(scope)

    with pytest.raises(HTTPException) as exc_info:
        verify_mtls_client_certificate(request)
    assert exc_info.value.status_code == 401
    assert "mTLS" in exc_info.value.detail


def test_verify_mtls_strict_mode_allowed_subjects(monkeypatch):
    """
    Проверяет фильтрацию по списку доверенных субъектов (ALLOWED_MTLS_SUBJECTS).
    """
    monkeypatch.setattr(settings, "REQUIRE_MTLS", True)
    monkeypatch.setattr(settings, "ALLOWED_MTLS_SUBJECTS", ["admin-client", "trusted-service"])

    # 1. Авторизованный субъект -> Успех
    auth_scope = {
        "type": "http",
        "headers": [
            (b"x-client-cert-verify", b"SUCCESS"),
            (b"x-client-cert-subject", b"CN=admin-client,O=FinancialAI"),
        ]
    }
    res = verify_mtls_client_certificate(Request(auth_scope))
    assert res["verified"] is True
    assert res["common_name"] == "admin-client"

    # 2. Неавторизованный субъект -> 403 Forbidden
    unauth_scope = {
        "type": "http",
        "headers": [
            (b"x-client-cert-verify", b"SUCCESS"),
            (b"x-client-cert-subject", b"CN=untrusted-client,O=FinancialAI"),
        ]
    }
    with pytest.raises(HTTPException) as exc_info:
        verify_mtls_client_certificate(Request(unauth_scope))
    assert exc_info.value.status_code == 403
    assert "не авторизован" in exc_info.value.detail


def test_verify_mtls_strict_mode_allowed_issuers(monkeypatch):
    """
    Проверяет фильтрацию по списку доверенных эмитентов (ALLOWED_MTLS_ISSUERS).
    """
    monkeypatch.setattr(settings, "REQUIRE_MTLS", True)
    monkeypatch.setattr(settings, "ALLOWED_MTLS_SUBJECTS", [])
    monkeypatch.setattr(settings, "ALLOWED_MTLS_ISSUERS", ["FinancialAI Internal Root CA"])

    # 1. Доверенный эмитент -> Успех
    auth_scope = {
        "type": "http",
        "headers": [
            (b"x-client-cert-verify", b"SUCCESS"),
            (b"x-client-cert-subject", b"CN=admin-client"),
            (b"x-client-cert-issuer", b"CN=FinancialAI Internal Root CA,O=FinancialAI"),
        ]
    }
    res = verify_mtls_client_certificate(Request(auth_scope))
    assert res["verified"] is True

    # 2. Недоверенный эмитент -> 403 Forbidden
    unauth_scope = {
        "type": "http",
        "headers": [
            (b"x-client-cert-verify", b"SUCCESS"),
            (b"x-client-cert-subject", b"CN=admin-client"),
            (b"x-client-cert-issuer", b"CN=Attacker Rogue CA"),
        ]
    }
    with pytest.raises(HTTPException) as exc_info:
        verify_mtls_client_certificate(Request(unauth_scope))
    assert exc_info.value.status_code == 403
    assert "доверенных центров" in exc_info.value.detail


# ------------------------------------------------------------------------------
# 4. Интеграционный тест эндпоинта /api/auth/mtls/status
# ------------------------------------------------------------------------------

def test_api_auth_mtls_status_endpoint(monkeypatch):
    """
    Проверяет работу HTTP-эндпоинта /api/auth/mtls/status через TestClient.
    """
    client = TestClient(app)

    # 1. Запрос без клиентского сертификата (режим permissive)
    monkeypatch.setattr(settings, "REQUIRE_MTLS", False)
    resp = client.get("/api/auth/mtls/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mtls_required"] is False
    assert data["verified"] is False
    assert data["is_present"] is False

    # 2. Запрос с mTLS-заголовками от Caddy
    headers = {
        "X-Client-Cert-Verify": "SUCCESS",
        "X-Client-Cert-Subject": "CN=admin-client,O=FinancialAI",
        "X-Client-Cert-Issuer": "CN=FinancialAI Internal Root CA",
        "X-Client-Cert-Serial": "20261005",
        "X-Client-Cert-Fingerprint": "E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855",
    }
    resp = client.get("/api/auth/mtls/status", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["verified"] is True
    assert data["common_name"] == "admin-client"
    assert data["serial"] == "20261005"
    assert data["fingerprint"] == "E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855"


# ------------------------------------------------------------------------------
# 5. Тесты параметров шифрования внутренних протоколов (In-Transit Encryption)
# ------------------------------------------------------------------------------

def test_in_transit_ssl_settings():
    """
    Проверяет наличие и корректность параметров шифрования для MinIO, Redis и PostgreSQL.
    """
    assert hasattr(settings, "MINIO_SECURE")
    assert hasattr(settings, "REDIS_SSL")
    assert hasattr(settings, "POSTGRES_SSLMODE")

    from db import database
    assert hasattr(database, "POSTGRES_SSLMODE")


def test_internal_certs_generation_script_exists():
    """
    Проверяет наличие скрипта генерации внутренних сертификатов и его базовую структуру.
    """
    script_path = Path(__file__).parent.parent / "scripts" / "generate_internal_certs.sh"
    assert script_path.exists(), "scripts/generate_internal_certs.sh не найден"

    content = script_path.read_text(encoding="utf-8")
    assert "#!/usr/bin/env bash" in content
    assert "openssl genrsa" in content
    assert "openssl req" in content
    assert "openssl x509" in content
    assert "ca.crt" in content
    assert "server.crt" in content
    assert "client.crt" in content
