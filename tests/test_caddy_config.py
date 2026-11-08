from pathlib import Path


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
    """Проверяет, что внешний и внутренний контуры принимают только TLS 1.3."""
    content = load_caddyfile()
    assert "protocols tls1.3" in content
    assert "protocols tls1.2 tls1.3" not in content


def test_caddyfile_client_auth_configuration():
    """Проверяет настройки mTLS (client_auth) с проверкой по доверенному Root CA."""
    content = load_caddyfile()
    assert "client_auth" in content, "Секция client_auth обязательна для Zero-Trust mTLS"
    assert "trusted_ca_cert_file /etc/caddy/certs/ca.crt" in content, "Сертификаты клиентов должны проверяться по ca.crt"
    assert "require_and_verify" in content or "CADDY_CLIENT_AUTH_MODE" in content, "Режим проверки должен быть строгим"


def test_caddyfile_mtls_headers_forwarded():
    """Проверяет, что проверенные метаданные клиентского сертификата пробрасываются в бэкенд и очищаются от подделки."""
    content = load_caddyfile()
    assert "header_up -X-Client-Cert-*" in content, "Caddyfile должен очищать входящие клиентские заголовки mTLS"
    assert "header_up X-Client-Cert-Verify {tls_client_verified}" in content
    assert "header_up X-Client-Cert-Subject {tls_client_subject}" in content
    assert "header_up X-Client-Cert-Issuer {tls_client_issuer}" in content
    assert "header_up X-Client-Cert-Serial {tls_client_serial}" in content
    assert "header_up X-Client-Cert-Fingerprint {tls_client_fingerprint}" in content


def test_caddyfile_internal_simulation_routing():
    """Проверяет межконтейнерный контур симуляции со строгим mTLS (client_auth)."""
    content = load_caddyfile()
    assert "simulation-api.internal" in content
    assert "client_auth" in content, "Внутренний маршрут должен использовать client_auth"
    assert "reverse_proxy https://simulation-api:8002" in content
    assert content.count("header_up X-Client-Cert-Serial {tls_client_serial}") >= 3


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


def test_internal_sites_bind_only_docker_networks():
    """Проверяет, что внутренние site block не слушают адрес опубликованного порта."""
    content = load_caddyfile()
    assert "172.16.0.0/12" not in content
    assert "bind 10.90.10.10" in content
    assert content.count("bind 10.90.20.10 10.90.30.10") == 2
    assert "remote_ip 10.90.10.1 10.90.20.1 10.90.30.1" in content
    assert "abort @docker_proxy" in content
    assert "not remote_ip 10.90.20.0/24 10.90.30.0/24" in content
    public_block = content.split("simulation-api.internal", 1)[0]
    assert "admin.fin-ai-agent.ru" in public_block
    assert "bind 10.90.20.10" not in public_block


def test_public_site_presents_platform_certificate_and_rejects_bank_client():
    """Проверяет сертификат платформы и отказ банку на публичном входе."""
    content = load_caddyfile()
    assert "tls /etc/caddy/admin/admin_server.crt /etc/caddy/admin/admin_server.key" in content
    assert "bank_service" in content
    assert "bank_operator" in content
    assert "bank-simulation" in content
    assert "abort @bank_client" in content


def test_simulation_proxy_keeps_internal_agent_headers():
    """Проверяет, что прокси симуляции снимает только заголовки, которые ставит Caddy."""
    content = load_caddyfile()
    simulation = content.split("simulation-api.internal", 1)[1].split("admin-server.internal", 1)[0]
    assert "header_up -X-Client-Cert-*" in simulation
    assert "header_up -X-Agent-Cert" not in simulation
    assert "header_up -X-Enclave-Cert" not in simulation
    assert "header_up -X-Signature" not in simulation
    assert "reverse_proxy https://simulation-api:8002" in simulation

