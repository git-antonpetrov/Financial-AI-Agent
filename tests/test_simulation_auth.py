import os
import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

from src.simulations.api.core.auth import (
    extract_token,
    verify_bank_token,
    verify_invest_token,
    verify_digital_token,
)


@pytest.fixture(autouse=True)
def setup_env(monkeypatch):
    monkeypatch.setenv("AGENT_BANK_BOOTSTRAP_TOKEN", "secret_bank_token_123")
    monkeypatch.setenv("AGENT_INVEST_BOOTSTRAP_TOKEN", "secret_invest_token_456")
    monkeypatch.setenv("AGENT_DIGITAL_BOOTSTRAP_TOKEN", "secret_digital_token_789")
    monkeypatch.setenv("AGENT_MAIN_BOOTSTRAP_TOKEN", "secret_main_orchestrator_000")


@pytest.fixture
def test_app():
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/bank/test", dependencies=[Depends(verify_bank_token)])
    def bank_test():
        return {"domain": "bank"}

    @app.get("/invest/test", dependencies=[Depends(verify_invest_token)])
    def invest_test():
        return {"domain": "invest"}

    @app.get("/ruble/test", dependencies=[Depends(verify_digital_token)])
    def ruble_test():
        return {"domain": "ruble"}

    return app


def test_public_health_check_requires_no_token(test_app):
    client = TestClient(test_app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_unauthorized_when_no_token_provided(test_app):
    client = TestClient(test_app)
    
    res_bank = client.get("/bank/test")
    assert res_bank.status_code == 401
    assert "Отсутствует токен аутентификации" in res_bank.json()["detail"]

    res_invest = client.get("/invest/test")
    assert res_invest.status_code == 401

    res_ruble = client.get("/ruble/test")
    assert res_ruble.status_code == 401


def test_unauthorized_when_invalid_token(test_app):
    client = TestClient(test_app)
    
    headers = {"X-Bootstrap-Token": "completely_fake_token"}
    response = client.get("/bank/test", headers=headers)
    assert response.status_code == 401
    assert "Недействительный токен" in response.json()["detail"]


def test_forbidden_cross_domain_access(test_app):
    client = TestClient(test_app)
    
    # Инвест-агент пытается стучаться в банк
    headers = {"X-Bootstrap-Token": "secret_invest_token_456"}
    response = client.get("/bank/test", headers=headers)
    assert response.status_code == 403
    assert "запрещен для данного агента" in response.json()["detail"]

    # Банковский агент пытается стучаться в цифровой рубль
    headers_bank = {"X-Bootstrap-Token": "secret_bank_token_123"}
    res_ruble = client.get("/ruble/test", headers=headers_bank)
    assert res_ruble.status_code == 403

    # Цифровой рубль пытается стучаться в инвестиции
    headers_digital = {"X-Bootstrap-Token": "secret_digital_token_789"}
    res_invest = client.get("/invest/test", headers=headers_digital)
    assert res_invest.status_code == 403


def test_successful_access_via_bootstrap_token_header(test_app):
    client = TestClient(test_app)
    
    # Банк
    res_bank = client.get("/bank/test", headers={"X-Bootstrap-Token": "secret_bank_token_123"})
    assert res_bank.status_code == 200
    assert res_bank.json() == {"domain": "bank"}

    # Инвест
    res_invest = client.get("/invest/test", headers={"X-Bootstrap-Token": "secret_invest_token_456"})
    assert res_invest.status_code == 200
    assert res_invest.json() == {"domain": "invest"}

    # Рубль
    res_ruble = client.get("/ruble/test", headers={"X-Bootstrap-Token": "secret_digital_token_789"})
    assert res_ruble.status_code == 200
    assert res_ruble.json() == {"domain": "ruble"}


def test_successful_access_via_authorization_bearer(test_app):
    client = TestClient(test_app)
    
    headers = {"Authorization": "Bearer secret_bank_token_123"}
    response = client.get("/bank/test", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"domain": "bank"}


def test_main_orchestrator_token_has_access_to_all_domains(test_app):
    client = TestClient(test_app)
    main_headers = {"X-Bootstrap-Token": "secret_main_orchestrator_000"}

    assert client.get("/bank/test", headers=main_headers).status_code == 200
    assert client.get("/invest/test", headers=main_headers).status_code == 200
    assert client.get("/ruble/test", headers=main_headers).status_code == 200


def test_server_configuration_error_when_token_unset(test_app, monkeypatch):
    client = TestClient(test_app)
    monkeypatch.delenv("AGENT_BANK_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_MAIN_BOOTSTRAP_TOKEN", raising=False)

    headers = {"X-Bootstrap-Token": "any_token"}
    response = client.get("/bank/test", headers=headers)
    assert response.status_code == 500
    assert "не настроены" in response.json()["detail"]


def test_real_simulation_app_endpoints_protected():
    from src.simulations.api.main import app as simulation_app

    client = TestClient(simulation_app)

    # Публичный health check
    assert client.get("/health").status_code == 200

    # Защищенные роуты симуляции без токена
    assert client.get("/bank/accounts/by-client/cl_123").status_code == 401
    assert client.get("/invest/strategies").status_code == 401
    assert client.get("/ruble/wallets/cl_123").status_code == 401

    # С неверным токеном
    bad_headers = {"X-Bootstrap-Token": "wrong_token"}
    assert client.get("/bank/accounts/by-client/cl_123", headers=bad_headers).status_code == 401
    assert client.get("/invest/strategies", headers=bad_headers).status_code == 401
    assert client.get("/ruble/wallets/cl_123", headers=bad_headers).status_code == 401

    # Междоменный доступ запрещен (403)
    invest_headers = {"X-Bootstrap-Token": "secret_invest_token_456"}
    assert client.get("/bank/accounts/by-client/cl_123", headers=invest_headers).status_code == 403
