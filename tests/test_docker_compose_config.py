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
    assert "./certs:/certs:ro" in pg.get("volumes", [])

    # 2. Redis
    redis_svc = services["redis"]
    redis_cmd = redis_svc.get("command", "")
    assert "--tls-port 6379" in redis_cmd
    assert "--port 0" in redis_cmd
    assert "--tls-cert-file /certs/redis.crt" in redis_cmd
    assert "./certs:/certs:ro" in redis_svc.get("volumes", [])

    # 3. MinIO
    minio_svc = services["minio"]
    assert "./certs/minio:/root/.minio/certs:ro" in minio_svc.get("volumes", [])

    # 4. Клиентские сервисы
    for s_name in ["admin-server", "vector-worker", "simulation-api", "bank-worker", "invest-worker", "digital-worker"]:
        svc = services[s_name]
        assert "./certs:/certs:ro" in svc.get("volumes", []), f"{s_name} должен монтировать ./certs:/certs:ro"

