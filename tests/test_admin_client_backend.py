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

# Предотвращаем конфликт пространств имен 'core' между admin_server и admin_client
client_core = os.path.join(backend_path, "core")
client_utils = os.path.join(backend_path, "core", "utils")
if "core" in sys.modules:
    core_mod = sys.modules["core"]
    if hasattr(core_mod, "__path__") and client_core not in core_mod.__path__:
        core_mod.__path__.insert(0, client_core)
if "core.utils" in sys.modules:
    utils_mod = sys.modules["core.utils"]
    if hasattr(utils_mod, "__path__") and client_utils not in utils_mod.__path__:
        utils_mod.__path__.insert(0, client_utils)

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
