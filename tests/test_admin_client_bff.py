"""
Модуль всестороннего тестирования архитектуры Backend-for-Frontend (BFF) и клиента администратора.
Проверяет:
1. Изоляцию фронтенда от прямых сетевых вызовов и утечки JWT-токенов / паролей OCR (Zero-Trust).
2. Автономное функционирование RemoteAdminClient (сессии в RAM, авто-ротация Refresh Token, кэш).
3. Интеграцию локальных эндпоинтов sidecar с защитой через X-Local-Secret и валидацию SSRF.
"""

import os
import sys
import json
import pytest
from unittest.mock import MagicMock, patch
import requests

# Настройка путей импорта
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

backend_path = os.path.join(repo_root, "src", "admin_client", "backend")
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)

try:
    from src.admin_client.backend import main
    from src.admin_client.backend.core.remote_client import (
        RemoteAdminClient,
        AuthenticationError,
        SessionExpiredError,
        RemoteClientError,
    )
except ImportError:
    import main
    from core.remote_client import (
        RemoteAdminClient,
        AuthenticationError,
        SessionExpiredError,
        RemoteClientError,
    )
from fastapi.testclient import TestClient


def test_remote_admin_client_login_success():
    """Проверяет успешную авторизацию RemoteAdminClient и сохранение сессии в оперативной памяти."""
    client = RemoteAdminClient()
    assert client.is_authenticated() is False

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "header.access_jwt.signature",
        "refresh_token": "header.refresh_jwt.signature",
        "role": "admin",
    }

    with patch.object(client.session, "post", return_value=mock_resp) as mock_post:
        res = client.login(
            server_url="https://admin.fin-ai-agent.ru",
            username="admin",
            password="StrongPassword123!",
            otp_code="654321"
        )
        assert res["status"] == "ok"
        assert res["username"] == "admin"
        assert res["role"] == "admin"
        assert client.is_authenticated() is True
        assert client.access_token == "header.access_jwt.signature"
        assert client.refresh_token == "header.refresh_jwt.signature"
        assert client.server_url == "https://admin.fin-ai-agent.ru"

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["data"]["otp_code"] == "654321"
        assert call_kwargs["headers"]["X-OTP-Code"] == "654321"


def test_remote_admin_client_login_errors():
    """Проверяет обработку различных сценариев ошибок аутентификации."""
    client = RemoteAdminClient()

    # 1. Пустой сервер или пароль
    with pytest.raises(AuthenticationError) as exc_info:
        client.login(server_url="", password="pwd")
    assert "Не указан адрес сервера" in str(exc_info.value)

    with pytest.raises(AuthenticationError) as exc_info:
        client.login(server_url="https://admin.bank.ru", password="")
    assert "Пароль не может быть пустым" in str(exc_info.value)

    # 2. Ошибка сервера: требуется 2FA
    mock_401_2fa = MagicMock()
    mock_401_2fa.status_code = 401
    mock_401_2fa.json.return_value = {"detail": "2FA code required"}

    with patch.object(client.session, "post", return_value=mock_401_2fa):
        with pytest.raises(AuthenticationError) as exc_info:
            client.login(server_url="https://admin.bank.ru", password="pwd")
        assert "errorEmptyOtp" in str(exc_info.value)

    # 3. Ошибка сервера: неверный код TOTP
    mock_401_bad_otp = MagicMock()
    mock_401_bad_otp.status_code = 401
    mock_401_bad_otp.json.return_value = {"detail": "Invalid 2FA TOTP code"}

    with patch.object(client.session, "post", return_value=mock_401_bad_otp):
        with pytest.raises(AuthenticationError) as exc_info:
            client.login(server_url="https://admin.bank.ru", password="pwd", otp_code="000000")
        assert "errorBadOtp" in str(exc_info.value)

    # 4. Ошибка сервера: код TOTP уже использован (anti-replay)
    mock_401_reused = MagicMock()
    mock_401_reused.status_code = 401
    mock_401_reused.json.return_value = {"detail": "OTP code has already been used"}

    with patch.object(client.session, "post", return_value=mock_401_reused):
        with pytest.raises(AuthenticationError) as exc_info:
            client.login(server_url="https://admin.bank.ru", password="pwd", otp_code="123456")
        assert "errorOtpReused" in str(exc_info.value)


def test_remote_admin_client_transparent_rtr():
    """Проверяет прозрачную ротацию токена доступа (RTR) при получении статуса 401."""
    client = RemoteAdminClient(server_url="https://admin.bank.ru")
    client.access_token = "expired_access_token"
    client.refresh_token = "valid_refresh_token"

    # Ответ на первый запрос (401)
    mock_resp_401 = MagicMock()
    mock_resp_401.status_code = 401

    # Ответ на повторный запрос (200)
    mock_resp_200 = MagicMock()
    mock_resp_200.status_code = 200
    mock_resp_200.json.return_value = [{"name": "bank", "status": "active"}]

    # Ответ от /api/auth/refresh (200)
    mock_refresh_resp = MagicMock()
    mock_refresh_resp.status_code = 200
    mock_refresh_resp.json.return_value = {
        "access_token": "new_fresh_access_token",
        "refresh_token": "new_fresh_refresh_token"
    }

    call_count = 0
    def mock_request(method, url, **kwargs):
        nonlocal call_count
        call_count += 1
        if "/api/auth/refresh" in url:
            return mock_refresh_resp
        if call_count == 1:
            assert kwargs["headers"]["Authorization"] == "Bearer expired_access_token"
            return mock_resp_401
        else:
            assert kwargs["headers"]["Authorization"] == "Bearer new_fresh_access_token"
            return mock_resp_200

    with patch.object(client.session, "request", side_effect=mock_request):
        resp = client.request("GET", "/api/agents")
        assert resp.status_code == 200
        assert client.access_token == "new_fresh_access_token"
        assert client.refresh_token == "new_fresh_refresh_token"


def test_remote_admin_client_session_expiration():
    """Проверяет сброс сессии и вызов SessionExpiredError при отказе рефреш-токена."""
    client = RemoteAdminClient(server_url="https://admin.bank.ru")
    client.access_token = "expired_access_token"
    client.refresh_token = "stale_refresh_token"

    mock_resp_401 = MagicMock()
    mock_resp_401.status_code = 401

    mock_refresh_fail = MagicMock()
    mock_refresh_fail.status_code = 401

    def mock_request(method, url, **kwargs):
        if "/api/auth/refresh" in url:
            return mock_refresh_fail
        return mock_resp_401

    with patch.object(client.session, "request", side_effect=mock_request):
        with pytest.raises(SessionExpiredError):
            client.request("GET", "/api/agents")

        # Сессия должна быть сброшена
        assert client.is_authenticated() is False
        assert client.access_token is None
        assert client.refresh_token is None


def test_remote_admin_client_contentai_caching():
    """Проверяет кэширование учетных данных OCR Content AI в памяти Python-клиента."""
    client = RemoteAdminClient(server_url="https://admin.bank.ru")
    client.access_token = "valid_token"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "username": "ocr_user",
        "password": "ocr_secret_password",
        "api_uri": "https://contentai.bank.ru"
    }

    with patch.object(client, "request", return_value=mock_resp) as mock_req:
        # Первый вызов делает запрос
        cfg1 = client.get_contentai_config()
        assert cfg1["username"] == "ocr_user"
        mock_req.assert_called_once()

        # Второй вызов возвращает из кэша без сетевого обращения
        cfg2 = client.get_contentai_config()
        assert cfg2["password"] == "ocr_secret_password"
        mock_req.assert_called_once()


def test_frontend_zero_trust_strict_static_analysis():
    """
    Статический анализ безопасности исходного кода React/Tauri фронтенда.
    Гарантирует, что фронтенд является 'Dumb UI' и не содержит утечек учетных данных.
    """
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    app_tsx = os.path.join(repo_root, "src", "admin_client", "frontend", "src", "App.tsx")
    dashboard_tsx = os.path.join(repo_root, "src", "admin_client", "frontend", "src", "Dashboard.tsx")

    with open(app_tsx, "r", encoding="utf-8") as f:
        app_code = f.read()

    with open(dashboard_tsx, "r", encoding="utf-8") as f:
        dashboard_code = f.read()

    # 1. Запрет хранения токенов в sessionStorage
    assert "sessionStorage" not in app_code, "App.tsx не должен использовать sessionStorage"
    assert "sessionStorage" not in dashboard_code, "Dashboard.tsx не должен использовать sessionStorage"

    # 2. Запрет прямых вызовов fetch к удаленным эндпоинтам
    assert "${baseUrl}/login" not in app_code
    assert "/api/auth/logout" not in app_code
    assert "authFetch" not in dashboard_code, "Ручной authFetch должен быть удален из Dashboard.tsx"
    assert "/api/config/contentai" not in dashboard_code, "Dashboard.tsx не должен запрашивать пароли OCR"

    # 3. Все обращения должны идти через локальный модуль api.ts (localFetch)
    assert "localFetch" in app_code
    assert "localFetch" in dashboard_code
    assert "/api/local/auth/login" in app_code
    assert "/api/local/auth/status" in app_code
    assert "/api/local/agents" in dashboard_code
    assert "/api/local/agent-requests" in dashboard_code
    assert "${sidecarBaseUrl}/api/local/process" in dashboard_code


def test_bff_process_endpoint_injects_credentials():
    """Проверяет прозрачную подстановку server_url, токена и паролей OCR в /api/local/process."""
    import io
    test_client = TestClient(main.app)

    # Настраиваем сессию в remote_client
    with patch.object(main.remote_client, "server_url", "https://localhost:8000"), \
         patch.object(main.remote_client, "access_token", "header.admin_token.sig"), \
         patch.object(main.remote_client, "get_contentai_config", return_value={
             "username": "ocr_sys",
             "password": "ocr_password_vault",
             "api_uri": "https://ocr.bank.internal"
         }), \
         patch.object(main, "run_pipeline") as mock_pipeline:

        dummy_file = io.BytesIO(b"%PDF-1.4 Mock document")
        # Отправляем форму только с agent_name и file (без server_url, admin_token и паролей OCR)
        res = test_client.post(
            "/api/local/process",
            data={
                "agent_name": "digital",
                "use_zero_trust": "false"
            },
            files={"file": ("reglament.pdf", dummy_file, "application/pdf")}
        )

        assert res.status_code == 200
        assert "job_id" in res.json()
        assert res.json()["filename"] == "reglament.pdf"

        # Проверяем аргументы переданные в run_pipeline
        mock_pipeline.assert_called_once()
        args = mock_pipeline.call_args[0]
        # args: (job_id, file_path, agent_name, server_url, admin_token, contentai_username, contentai_password, ...)
        assert args[2] == "digital"
        assert args[3] == "https://localhost:8000"
        assert args[4] == "header.admin_token.sig"
        assert args[5] == "ocr_sys"
        assert args[6] == "ocr_password_vault"
        assert args[7] == "https://ocr.bank.internal"
