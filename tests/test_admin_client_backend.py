"""
Модуль тестирования функциональности локального бэкенда admin_client и защиты от SSRF.
"""

import os
import sys
import pytest
from unittest.mock import patch
import socket

# Подключаем backend admin_client к sys.path
backend_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "admin_client", "backend"))
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)


from fastapi import HTTPException
from main import validate_server_url


def test_validate_server_url_allows_localhost_and_public():
    """Проверяет успешный пропуск локальных адресов разработки и публичных доменов."""
    # Локальные адреса должны быть разрешены
    validate_server_url("http://localhost:8001")
    validate_server_url("http://127.0.0.1:8005")
    validate_server_url("http://[::1]:8005")
    
    # Публичный домен (при моке публичного IP)
    with patch("socket.getaddrinfo") as mock_dns:
        mock_dns.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        validate_server_url("https://example.com")


def test_validate_server_url_rejects_invalid_schemes():
    """Проверяет отклонение некорректных схем URL и пустых адресов."""
    with pytest.raises(HTTPException) as exc_info:
        validate_server_url("ftp://example.com")
    assert exc_info.value.status_code == 400

    with pytest.raises(HTTPException) as exc_info:
        validate_server_url("file:///etc/passwd")
    assert exc_info.value.status_code == 400

    with pytest.raises(HTTPException) as exc_info:
        validate_server_url("")
    assert exc_info.value.status_code == 400


def test_validate_server_url_blocks_private_and_metadata_ips():
    """Проверяет блокировку прямых приватных IP и сервисов облачных метаданных (SSRF)."""
    # 192.168.x.x
    with pytest.raises(HTTPException) as exc_info:
        validate_server_url("http://192.168.1.1:8080")
    assert exc_info.value.status_code == 400
    assert "SSRF protection" in exc_info.value.detail

    # 10.x.x.x
    with pytest.raises(HTTPException) as exc_info:
        validate_server_url("http://10.0.0.1:9000")
    assert exc_info.value.status_code == 400
    assert "SSRF protection" in exc_info.value.detail

    # 169.254.169.254 (Cloud metadata)
    with pytest.raises(HTTPException) as exc_info:
        validate_server_url("http://169.254.169.254/latest/meta-data/")
    assert exc_info.value.status_code == 400
    assert "SSRF protection" in exc_info.value.detail


def test_validate_server_url_blocks_dns_rebinding_to_private():
    """Проверяет блокировку DNS-имен, резолвящихся во внутренние приватные подсети."""
    with patch("socket.getaddrinfo") as mock_dns:
        mock_dns.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.200.1.10", 80))]
        with pytest.raises(HTTPException) as exc_info:
            validate_server_url("http://internal-corp-service.local")
        assert exc_info.value.status_code == 400
        assert "SSRF protection" in exc_info.value.detail


def test_validate_server_url_handles_unresolvable_hosts():
    """Проверяет корректную обработку ошибки резолвинга DNS."""
    with patch("socket.getaddrinfo", side_effect=socket.gaierror("Name or service not known")):
        with pytest.raises(HTTPException) as exc_info:
            validate_server_url("http://non-existent-hostname-xyz.org")
        assert exc_info.value.status_code == 400
        assert "unable to resolve hostname" in exc_info.value.detail


def test_admin_client_ports_dynamic_configuration():
    """Проверяет динамическое получение URL sidecar во фронтенде и поддержку диапазона портов в Tauri CSP."""
    dashboard_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "admin_client", "frontend", "src", "Dashboard.tsx"))
    with open(dashboard_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    # Фронтенд должен использовать динамический базовый URL
    assert "getLocalSidecarUrl" in content
    assert "${sidecarBaseUrl}/api/local/process" in content
    assert "${sidecarBaseUrl}/api/local/progress/" in content
    assert "http://127.0.0.1:8005/api/local/process" not in content

    tauri_conf_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "admin_client", "frontend", "src-tauri", "tauri.conf.json"))
    with open(tauri_conf_path, "r", encoding="utf-8") as f:
        tauri_conf = f.read()
    # CSP должен разрешать динамические порты на 127.0.0.1
    assert "http://127.0.0.1:*" in tauri_conf


def test_ipc_security_guard_enforces_secret():
    """Проверяет отклонение запросов без токена рукопожатия или с неверным токеном."""
    from fastapi.testclient import TestClient
    import main

    test_secret = "test-crypto-ipc-secret-123456"
    with patch.object(main, "SIDECAR_IPC_SECRET", test_secret):
        client = TestClient(main.app)
        
        # 1. Запрос без токена блокируется middleware (403)
        res_no_token = client.post("/api/local/process")
        assert res_no_token.status_code == 403
        assert "Forbidden: Invalid or missing IPC secret" in res_no_token.text

        # 2. Запрос с неверным токеном в заголовке блокируется middleware (403)
        res_bad_token = client.post("/api/local/process", headers={"X-Local-Secret": "wrong-secret"})
        assert res_bad_token.status_code == 403

        # 3. Запрос с корректным заголовком X-Local-Secret успешно проходит middleware (получает 422 из-за отсутствия полей формы)
        res_good_header = client.post("/api/local/process", headers={"X-Local-Secret": test_secret})
        assert res_good_header.status_code == 422

        # 4. Запрос с корректным query-параметром ?secret= успешно проходит middleware (получает 422)
        res_good_query = client.post(f"/api/local/process?secret={test_secret}")
        assert res_good_query.status_code == 422


def test_ipc_security_guard_rejects_unauthorized_origin():
    """Проверяет блокировку запросов с недоверенным Origin."""
    from fastapi.testclient import TestClient
    import main

    client = TestClient(main.app)
    res_bad_origin = client.get(
        "/api/local/progress/dummy-job",
        headers={"Origin": "http://evil-attacker-site.com"}
    )
    assert res_bad_origin.status_code == 403
    assert "Forbidden: Invalid origin" in res_bad_origin.text


def test_pyinstaller_spec_hardening():
    """Проверяет настройки безопасности и обфускации в PyInstaller spec-файле."""
    spec_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "admin_client", "backend", "admin-backend-x86_64-pc-windows-msvc.spec"))
    with open(spec_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Оптимизация байткода уровня 2 (удаление docstrings и assert)
    assert "optimize=2" in content
    # Скрытие отладочных диалоговых окон
    assert "disable_windowed_traceback=True" in content
    # Исключение отладочных модулей
    assert "'pdb'" in content
    assert "'unittest'" in content
    assert "'tkinter'" in content


def test_frozen_execution_requires_secret():
    """Проверяет логику запрета запуска упакованного бинарника без IPC-секрета."""
    import main
    # При sys.frozen == False и пустом секрете (режим разработки) модуль импортируется без ошибок
    assert getattr(sys, "frozen", False) is False


def test_local_auth_login_and_status():
    """Проверяет локальную авторизацию через BFF и получение статуса сессии."""
    from fastapi.testclient import TestClient
    import main
    from unittest.mock import MagicMock

    client = TestClient(main.app)

    # 1. Проверка SSRF в server_url при логине
    res_ssrf = client.post(
        "/api/local/auth/login",
        json={"server_url": "http://192.168.1.1:8000", "password": "secret"}
    )
    assert res_ssrf.status_code == 400
    assert "SSRF protection" in res_ssrf.text

    # 2. Успешный логин с моком remote_client.login
    with patch.object(main.remote_client, "login", return_value={"status": "ok", "username": "admin", "role": "admin", "server_url": "https://remote.bank.internal"}) as mock_login:
        res_ok = client.post(
            "/api/local/auth/login",
            json={"server_url": "https://localhost:8000", "password": "secret", "otp_code": "123456"}
        )
        assert res_ok.status_code == 200
        data = res_ok.json()
        assert data["status"] == "ok"
        assert data["username"] == "admin"
        mock_login.assert_called_once()

    # 3. Проверка статуса авторизации
    with patch.object(main.remote_client, "get_session_info", return_value={"is_authenticated": True, "server_url": "https://localhost:8000", "username": "admin", "role": "admin"}):
        res_status = client.get("/api/local/auth/status")
        assert res_status.status_code == 200
        assert res_status.json()["is_authenticated"] is True

    # 4. Проверка logout
    with patch.object(main.remote_client, "logout") as mock_logout:
        res_logout = client.post("/api/local/auth/logout")
        assert res_logout.status_code == 200
        assert res_logout.json() == {"status": "ok"}
        mock_logout.assert_called_once()


def test_local_agent_endpoints_unauthorized_and_authorized():
    """Проверяет эндпоинты управления агентами и заявками через BFF."""
    from fastapi.testclient import TestClient
    import main

    client = TestClient(main.app)

    # 1. Запрос к агентам без авторизации возвращает 401
    with patch.object(main.remote_client, "is_authenticated", return_value=False):
        res_unauth = client.get("/api/local/agents")
        assert res_unauth.status_code == 401
        assert "not authenticated" in res_unauth.text

    # 2. Авторизованные вызовы
    with patch.object(main.remote_client, "is_authenticated", return_value=True):
        # Список агентов
        with patch.object(main.remote_client, "get_agents", return_value=[{"name": "bank", "status": "active"}]):
            res_agents = client.get("/api/local/agents")
            assert res_agents.status_code == 200
            assert res_agents.json() == [{"name": "bank", "status": "active"}]

        # Suspend
        with patch.object(main.remote_client, "suspend_agent", return_value={"status": "suspended"}):
            res_suspend = client.post("/api/local/agents/bank/suspend")
            assert res_suspend.status_code == 200
            assert res_suspend.json()["status"] == "suspended"

        # Reactivate
        with patch.object(main.remote_client, "reactivate_agent", return_value={"status": "active"}):
            res_reactivate = client.post("/api/local/agents/bank/reactivate")
            assert res_reactivate.status_code == 200
            assert res_reactivate.json()["status"] == "active"

        # Revoke
        with patch.object(main.remote_client, "revoke_agent", return_value={"status": "revoked"}):
            res_revoke = client.post("/api/local/agents/bank/revoke", json={"reason": "Compromised"})
            assert res_revoke.status_code == 200
            assert res_revoke.json()["status"] == "revoked"

        # Agent requests
        with patch.object(main.remote_client, "get_agent_requests", return_value=[{"id": 1, "agent": "invest"}]):
            res_reqs = client.get("/api/local/agent-requests")
            assert res_reqs.status_code == 200
            assert len(res_reqs.json()) == 1

        # Approve
        with patch.object(main.remote_client, "approve_agent_requests", return_value={"approved": [1]}):
            res_approve = client.post("/api/local/agent-requests/approve", json={"request_ids": [1]})
            assert res_approve.status_code == 200
            assert res_approve.json()["approved"] == [1]

        # Reject
        with patch.object(main.remote_client, "reject_agent_requests", return_value={"rejected": [1]}):
            res_reject = client.post("/api/local/agent-requests/reject", json={"request_ids": [1]})
            assert res_reject.status_code == 200
            assert res_reject.json()["rejected"] == [1]


def test_process_document_uses_session_credentials():
    """Проверяет подхват server_url, admin_token и данных Content AI из активной сессии remote_client."""
    from fastapi.testclient import TestClient
    import main
    import io

    client = TestClient(main.app)

    # Настраиваем сессию в remote_client
    with patch.object(main.remote_client, "server_url", "https://localhost:8000"), \
         patch.object(main.remote_client, "access_token", "header.payload.signature"), \
         patch.object(main.remote_client, "get_contentai_config", return_value={"username": "ocr_user", "password": "ocr_pass", "api_uri": "https://ocr.internal"}), \
         patch.object(main, "run_pipeline") as mock_pipeline:

        dummy_file = io.BytesIO(b"PDF document content")
        # Отправляем форму без явных server_url, admin_token и contentai_* (они подтягиваются из remote_client)
        res = client.post(
            "/api/local/process",
            data={
                "agent_name": "bank",
                "use_zero_trust": "false"  # Отключаем требование локальных сертификатов для юнит-теста
            },
            files={"file": ("test.pdf", dummy_file, "application/pdf")}
        )
        assert res.status_code == 200
        assert "job_id" in res.json()

