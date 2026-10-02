import pytest
from fastapi.testclient import TestClient
from src.simulations.api.main import app


def test_cors_preflight_allowed_origin():
    client = TestClient(app)
    headers = {
        "Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "X-Bootstrap-Token,Content-Type",
    }
    response = client.options("/health", headers=headers)
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert response.headers.get("access-control-allow-credentials") == "true"
    # Проверяет отсутствие универсального подстановочного источника wildcard (*)
    assert response.headers.get("access-control-allow-origin") != "*"


def test_cors_preflight_tauri_origin():
    client = TestClient(app)
    headers = {
        "Origin": "tauri://localhost",
        "Access-Control-Request-Method": "GET",
    }
    response = client.options("/health", headers=headers)
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "tauri://localhost"
    assert response.headers.get("access-control-allow-credentials") == "true"


def test_cors_preflight_disallowed_origin():
    client = TestClient(app)
    headers = {
        "Origin": "http://malicious-attacker-website.com",
        "Access-Control-Request-Method": "POST",
    }
    response = client.options("/health", headers=headers)
    # При запрещенном Origin CORS middleware не возвращает allow-origin
    assert "access-control-allow-origin" not in response.headers


def test_cors_simple_request_disallowed_origin():
    client = TestClient(app)
    headers = {"Origin": "http://evil-phishing.com"}
    response = client.get("/health", headers=headers)
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
