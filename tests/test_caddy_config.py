from pathlib import Path
import pytest


def load_caddyfile() -> str:
    """Загружает содержимое Caddyfile из проекта."""
    caddy_path = Path(__file__).parent.parent / "src" / "caddy" / "Caddyfile"
    assert caddy_path.exists(), f"Caddyfile не найден по пути {caddy_path}"
    return caddy_path.read_text(encoding="utf-8")


def test_caddyfile_exists_and_contains_domains():
    """Проверяет наличие Caddyfile и базовых маршрутов для админа и симуляции."""
    content = load_caddyfile()
    assert "admin.fin-ai-agent" in content, "Caddyfile должен содержать маршрут для домена администратора"
    assert "simulation-api.internal" in content, "Caddyfile должен содержать внутренний маршрут simulation-api.internal"


def test_caddyfile_tls_protocols_and_ciphers():
    """Проверяет строгие настройки TLS: протоколы 1.2/1.3 и надежные наборы шифров."""
    content = load_caddyfile()
    assert "protocols tls1.2 tls1.3" in content, "Должны быть разрешены только TLS 1.2 и TLS 1.3"
    assert "TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384" in content
    assert "TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384" in content
    assert "TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256" in content


def test_caddyfile_client_auth_configuration():
    """Проверяет настройки mTLS (client_auth) с проверкой по доверенному Root CA."""
    content = load_caddyfile()
    assert "client_auth" in content, "Секция client_auth обязательна для Zero-Trust mTLS"
    assert "trusted_ca_cert_file /etc/caddy/certs/ca.crt" in content, "Сертификаты клиентов должны проверяться по ca.crt"
    assert "require_and_verify" in content or "CADDY_CLIENT_AUTH_MODE" in content, "Режим проверки должен быть строгим"


def test_caddyfile_mtls_headers_forwarded():
    """Проверяет, что проверенные метаданные клиентского сертификата пробрасываются в бэкенд."""
    content = load_caddyfile()
    assert "header_up X-Client-Cert-Verify {tls_client_verified}" in content
    assert "header_up X-Client-Cert-Subject {tls_client_subject}" in content
    assert "header_up X-Client-Cert-Issuer {tls_client_issuer}" in content
    assert "header_up X-Client-Cert-Serial {tls_client_serial}" in content
    assert "header_up X-Client-Cert-Fingerprint {tls_client_fingerprint}" in content


def test_caddyfile_internal_simulation_routing():
    """Проверяет межконтейнерный контур симуляции с автоматическим tls internal."""
    content = load_caddyfile()
    assert "simulation-api.internal" in content
    assert "tls internal" in content, "Внутренний маршрут должен использовать tls internal"
    assert "reverse_proxy simulation-api:8002" in content


def test_caddyfile_security_headers():
    """Проверяет заголовки безопасности HTTP (HSTS, nosniff, DENY, скрытие Server)."""
    content = load_caddyfile()
    assert "Strict-Transport-Security" in content
    assert "X-Content-Type-Options \"nosniff\"" in content
    assert "X-Frame-Options \"DENY\"" in content
    assert "-Server" in content, "Заголовок Server должен быть скрыт"


def test_caddyfile_streaming_timeouts():
    """Проверяет увеличенные таймауты для LLM и SSE streaming (180 секунд)."""
    content = load_caddyfile()
    assert "response_header_timeout 180s" in content
    assert "read_timeout 180s" in content
    assert "write_timeout 180s" in content


def test_caddyfile_internal_inter_service_tls_routing():
    """Проверяет шифрование межсервисного транзита: bank-service.internal и admin-server.internal."""
    content = load_caddyfile()
    assert "bank-service.internal" in content
    assert "admin-server.internal" in content
    assert "reverse_proxy admin-server:8000" in content

