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
