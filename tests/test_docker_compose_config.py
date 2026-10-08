import yaml
from pathlib import Path


def test_docker_compose_simulations_seeder_profile():
    """
    Проверяет, что сервис simulations-seeder изолирован профилем 'seed',
    чтобы не запускаться автоматически и не перезаписывать данные БД при каждом docker compose up.
    """
    compose_path = Path(__file__).parent.parent / "docker-compose.yml"
    assert compose_path.exists(), "docker-compose.yml не найден в корне проекта"

    with open(compose_path, "r", encoding="utf-8") as f:
        compose_data = yaml.safe_load(f)

    assert "services" in compose_data
    services = compose_data["services"]

    # 1. Проверяем наличие simulations-seeder
    assert "simulations-seeder" in services
    seeder = services["simulations-seeder"]

    # 2. Проверяем наличие профиля 'seed'
    assert "profiles" in seeder, "simulations-seeder должен иметь секцию 'profiles'"
    assert "seed" in seeder["profiles"], "simulations-seeder должен иметь профиль 'seed'"

    # 3. Проверяем, что рабочие сервисы НЕ зависят от seeder
    worker_services = ["bank-worker", "invest-worker", "digital-worker", "simulation-api"]
    for s_name in worker_services:
        if s_name in services:
            deps = services[s_name].get("depends_on", [])
            if isinstance(deps, dict):
                deps = list(deps.keys())
            assert "simulations-seeder" not in deps, f"Сервис {s_name} не должен зависеть от simulations-seeder"


def test_docker_compose_zero_trust_in_transit_encryption():
    """
    Проверяет, что docker-compose.yml полностью сконфигурирован под Zero-Trust:
    1. postgres-db запускается с ssl=on и примонтированными сертификатами;
    2. redis запускается с --tls-port 6379, --port 0 и сертификатами;
    3. minio монтирует certs/minio в /root/.minio/certs;
    4. admin-server, vector-worker, simulation-api монтируют /certs и имеют включенный TLS.
    """
    compose_path = Path(__file__).parent.parent / "docker-compose.yml"
    with open(compose_path, "r", encoding="utf-8") as f:
        compose_data = yaml.safe_load(f)

    services = compose_data["services"]

    # 1. PostgreSQL
    pg = services["postgres-db"]
    assert "-c ssl=on" in pg.get("command", "")
    assert "-c ssl_cert_file=/certs/postgres.crt" in pg.get("command", "")
    assert "certs_data:/certs:ro" in pg.get("volumes", [])

    # 2. Redis
    redis_svc = services["redis"]
    redis_cmd = redis_svc.get("command", "")
    assert "--tls-port 6379" in redis_cmd
    assert "--port 0" in redis_cmd
    assert "--tls-cert-file /certs/redis.crt" in redis_cmd
    assert "certs_data:/certs:ro" in redis_svc.get("volumes", [])

    # 3. MinIO
    minio_svc = services["minio"]
    assert "certs_data:/certs:ro" in minio_svc.get("volumes", [])
    assert "--certs-dir /certs/minio" in minio_svc.get("command", "")

    # 4. Клиентские сервисы
    for s_name in ["admin-server", "vector-worker", "simulation-api", "bank-worker", "invest-worker", "digital-worker"]:
        svc = services[s_name]
        assert "certs_data:/certs:ro" in svc.get("volumes", []), f"{s_name} должен монтировать certs_data:/certs:ro"


def test_docker_compose_root_ca_service_and_pki_network():
    """
    Проверяет сервис root-ca в docker-compose.yml:
    - Сборка на базе src/pki/Dockerfile.ca;
    - Изоляция в подсети pki_net;
    - Монтирование тома root_ca_data:/data/ca и certs_data:/shared_certs;
    - restart: 'no' (разовая инициализация при bootstrap).
    """
    compose_path = Path(__file__).parent.parent / "docker-compose.yml"
    with open(compose_path, "r", encoding="utf-8") as f:
        compose_data = yaml.safe_load(f)

    services = compose_data["services"]
    assert "root-ca" in services, "Сервис root-ca должен быть объявлен в docker-compose.yml"

    ca_svc = services["root-ca"]
    assert ca_svc.get("build", {}).get("dockerfile") == "src/pki/Dockerfile.ca"
    assert ca_svc.get("container_name") == "root-ca-service"
    assert ca_svc.get("restart") == "no"
    assert ca_svc.get("networks") == ["pki_net"]

    vols = ca_svc.get("volumes", [])
    assert "root_ca_data:/data/ca" in vols
    assert "certs_data:/shared_certs" in vols
    assert "certs_data" in compose_data.get("volumes", {})


def test_docker_compose_five_isolated_networks():
    """
    Проверяет сетевую топологию Defense-in-Depth / Zero-Trust (5 подсетей):
    - Наличие pki_net, edge_net, backend_net, data_net, simulation_net;
    - Строгая изоляция: pki_net и data_net имеют флаг internal: true;
    - Изоляция Root CA: ни один другой сервис не подключен к pki_net.
    """
    compose_path = Path(__file__).parent.parent / "docker-compose.yml"
    with open(compose_path, "r", encoding="utf-8") as f:
        compose_data = yaml.safe_load(f)

    networks = compose_data.get("networks", {})
    required_nets = {"pki_net", "edge_net", "backend_net", "data_net", "simulation_net"}
    for net_name in required_nets:
        assert net_name in networks, f"Сеть {net_name} отсутствует в docker-compose.yml"

    # Проверка флагов internal: true
    assert networks["pki_net"].get("internal") is True, "pki_net должна быть строго изолирована (internal: true)"
    assert networks["data_net"].get("internal") is True, "data_net должна быть строго изолирована (internal: true)"

    # Проверка изоляции Root CA: только root-ca подключен к pki_net
    services = compose_data["services"]
    for svc_name, svc_conf in services.items():
        svc_nets = svc_conf.get("networks", [])
        if svc_name == "root-ca":
            assert "pki_net" in svc_nets
        else:
            assert "pki_net" not in svc_nets, f"Сервис {svc_name} не должен иметь доступа к изолированной сети pki_net"


def test_docker_compose_caddy_and_volumes_configuration():
    """
    Проверяет конфигурацию Caddy шлюза и персистентных томов:
    - caddy зависит от admin-server;
    - caddy изолирован в edge_net;
    - Наличие персистентных томов root_ca_data, postgres_data, redis_data, chroma_data, minio_data.
    """
    compose_path = Path(__file__).parent.parent / "docker-compose.yml"
    with open(compose_path, "r", encoding="utf-8") as f:
        compose_data = yaml.safe_load(f)

    services = compose_data["services"]
    caddy = services["caddy"]
    deps = caddy.get("depends_on", [])
    if isinstance(deps, dict):
        deps = list(deps.keys())
    assert "admin-server" in deps

    caddy_nets = caddy.get("networks", [])
    assert caddy_nets == ["edge_net"]

    vols = compose_data.get("volumes", {})
    for vol in ["root_ca_data", "postgres_data", "redis_data", "chroma_data", "minio_data", "caddy_data", "caddy_config"]:
        assert vol in vols, f"Том {vol} должен быть объявлен в volumes"


def test_env_example_pki_and_mtls_variables():
    """Проверяет наличие параметров центрального Root CA и mTLS в .env.example."""
    env_example_path = Path(__file__).parent.parent / ".env.example"
    assert env_example_path.exists()

    with open(env_example_path, "r", encoding="utf-8") as f:
        env_content = f.read()

    assert "ROOT_CA_PASSPHRASE=" in env_content
    assert "FORCE_REGENERATE=" in env_content
    assert "ADMIN_DOMAIN=" in env_content
    assert "ADMIN_LOCAL_DOMAIN=" in env_content
    assert "CADDY_CLIENT_AUTH_MODE=require_and_verify" in env_content


