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


def test_admin_client_ports_consistency():
    """Проверяет соответствие портов sidecar (8005) во фронтенде и конфигурации Tauri."""
    dashboard_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "admin_client", "frontend", "src", "Dashboard.tsx"))
    with open(dashboard_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    # Должен обращаться к порту 8005 для локального sidecar
    assert "http://127.0.0.1:8005/api/local/process" in content
    assert "http://127.0.0.1:8005/api/local/progress/" in content
    assert "8001/api/local/" not in content

    tauri_conf_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "admin_client", "frontend", "src-tauri", "tauri.conf.json"))
    with open(tauri_conf_path, "r", encoding="utf-8") as f:
        tauri_conf = f.read()
    assert "http://127.0.0.1:8005" in tauri_conf
