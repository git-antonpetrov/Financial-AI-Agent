"""Проверяет секрет прокси шлюза симуляции и отказ старта на плейсхолдерах."""

import os

import pytest
from fastapi.testclient import TestClient

from src.simulations.api.main import (
    app,
    request_has_valid_proxy_secret,
    validate_simulation_security,
)


def test_proxy_secret_rejects_missing_and_wrong_header():
    """Запрос без верного X-Proxy-Secret не проходит, кроме loopback /health."""
    secret = os.environ["PROXY_INTERNAL_SECRET"]
    assert request_has_valid_proxy_secret("/bank/accounts", "10.90.30.10", None) is False
    assert request_has_valid_proxy_secret("/bank/accounts", "10.90.30.10", "forged") is False
    assert request_has_valid_proxy_secret("/health", "10.90.30.10", None) is False
    assert request_has_valid_proxy_secret("/health", "127.0.0.1", None) is True
    assert request_has_valid_proxy_secret("/health", "::1", None) is True
    assert request_has_valid_proxy_secret("/bank/accounts", "127.0.0.1", secret) is True

    client = TestClient(app)
    missing = client.get("/bank/accounts/by-client/cl_123")
    assert missing.status_code == 401
    assert missing.json()["detail"] == "Неверный или отсутствующий X-Proxy-Secret"

    forged = client.get(
        "/health",
        headers={"X-Proxy-Secret": "forged-untrusted-proxy-token"},
    )
    assert forged.status_code == 401

    accepted = client.get("/health", headers={"X-Proxy-Secret": secret})
    assert accepted.status_code == 200


def test_loopback_health_stays_open_without_secret():
    """Healthcheck контейнера ходит на loopback и не передаёт секрет прокси."""
    client = TestClient(app, client=("127.0.0.1", 8002))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_strict_start_rejects_empty_proxy_secret(monkeypatch):
    """Пустой PROXY_INTERNAL_SECRET при STRICT_SECURITY останавливает старт."""
    monkeypatch.setenv("STRICT_SECURITY", "true")
    monkeypatch.setenv("REQUIRE_ENCLAVE_SIGNATURE", "true")
    monkeypatch.setenv("REQUIRE_ORACLE_SIGNATURE", "true")
    monkeypatch.setenv("PROXY_INTERNAL_SECRET", "")
    with pytest.raises(RuntimeError, match="PROXY_INTERNAL_SECRET must be set"):
        validate_simulation_security()


def test_strict_start_rejects_example_placeholder(monkeypatch):
    """Старт шлюза отвергает плейсхолдер секрета, когда строгий режим включён."""
    monkeypatch.setenv("STRICT_SECURITY", "true")
    monkeypatch.setenv("REQUIRE_ENCLAVE_SIGNATURE", "true")
    monkeypatch.setenv("REQUIRE_ORACLE_SIGNATURE", "true")
    monkeypatch.setenv("PROXY_INTERNAL_SECRET", "unit-proxy-secret-not-from-example")
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", "fedcba9876543210" * 4)
    monkeypatch.setenv("ADMIN_TOTP_SECRET", "MFRGGZDFMZTWQ2LKMFRGGZDFMZTWQ2LK")
    monkeypatch.setenv("REDIS_PASSWORD", "change_this_redis_password")
    with pytest.raises(RuntimeError, match="detected in REDIS_PASSWORD"):
        validate_simulation_security()
