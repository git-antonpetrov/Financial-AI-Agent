import os
import shutil
from pathlib import Path
import pytest

from scripts.setup_vds_security_env import setup_vds
from scripts.deploy_vds_prod import find_docker_compose_cmd, run_deployment


def test_setup_vds_security_env_creates_and_hardens_env(tmp_path):
    """
    Проверяет корректность инициализации переменных окружения и ключей:
    - генерация RSA пары (jwt_private.pem, jwt_public.pem);
    - замена плейсхолдеров на криптостойкие секреты;
    - принудительное включение параметров сквозного Zero-Trust шифрования.
    """
    root_dir = Path(__file__).resolve().parent.parent
    example_env = root_dir / ".env.example"
    assert example_env.exists(), ".env.example должен существовать"

    # Копируем .env.example во временную директорию
    shutil.copy(example_env, tmp_path / ".env.example")

    info = setup_vds(str(tmp_path))
    assert isinstance(info, dict)

    # 1. Проверяем генерацию RSA ключей
    jwt_priv = tmp_path / "certs" / "jwt_private.pem"
    jwt_pub = tmp_path / "certs" / "jwt_public.pem"
    assert jwt_priv.exists(), "jwt_private.pem должен быть создан"
    assert jwt_pub.exists(), "jwt_public.pem должен быть создан"
    assert "-----BEGIN " in jwt_priv.read_text(encoding="utf-8")
    assert "-----BEGIN PUBLIC KEY-----" in jwt_pub.read_text(encoding="utf-8")

    # 2. Проверяем содержимое созданного .env
    env_file = tmp_path / ".env"
    assert env_file.exists(), ".env должен быть создан"
    env_content = env_file.read_text(encoding="utf-8")

    # Zero-Trust TLS параметры
    assert "POSTGRES_SSLMODE=require" in env_content
    assert "REDIS_SSL=true" in env_content
    assert "MINIO_SECURE=true" in env_content
    assert "MINIO_SSE_ENABLED=true" in env_content
    assert "CHROMA_ENVELOPE_ENCRYPTION_ENABLED=true" in env_content

    # Проверяем замену плейсхолдеров
    assert "change_this_strong_password_in_production" not in env_content
    assert "change_this_redis_password" not in env_content
    assert "DATA_ENCRYPTION_KEY=0123456789abcdef" not in env_content
    assert "MINIO_KMS_SECRET_KEY=financial-kms-key:0123456789abcdef" not in env_content
    assert "ADMIN_TOTP_SECRET=JBSWY3DPEHPK3PXP" not in env_content


def test_deploy_vds_prod_bash_script_structure():
    """
    Проверяет целостность и синтаксическую структуру scripts/deploy_vds_prod.sh:
    - шебанг bash и безопасный режим -euo pipefail;
    - вызов шагов: setup_vds_security_env.py, generate_internal_certs, docker compose up -d;
    - проверка сертификатов и контроль healthcheck.
    """
    root_dir = Path(__file__).resolve().parent.parent
    sh_path = root_dir / "scripts" / "deploy_vds_prod.sh"
    assert sh_path.exists(), "scripts/deploy_vds_prod.sh должен присутствовать"

    content = sh_path.read_text(encoding="utf-8")
    assert "#!/usr/bin/env bash" in content
    assert "set -euo pipefail" in content
    assert "setup_vds_security_env.py" in content
    assert "generate_internal_certs.py" in content
    assert "up -d --build" in content
    assert "DOCKER_COMPOSE_CMD" in content
    assert "certs/ca.crt" in content
    assert "certs/postgres.crt" in content
    assert "certs/redis.crt" in content
    assert "certs/minio.crt" in content
    assert "certs/client.p12" in content
    assert "chmod 600" in content
    assert "admin-server" in content


def test_deploy_vds_prod_python_orchestrator_no_docker(tmp_path, monkeypatch):
    """
    Проверяет выполнение сценария развертывания через Python скрипт deploy_vds_prod.py
    в режиме --no-docker без запуска контейнеров.
    """
    root_dir = Path(__file__).resolve().parent.parent
    example_env = root_dir / ".env.example"
    shutil.copy(example_env, tmp_path / ".env.example")

    import scripts.deploy_vds_prod as deploy_mod
    monkeypatch.setattr(deploy_mod, "PROJECT_ROOT", tmp_path)

    # Запуск этапов подготовки без вызова Docker
    deploy_mod.run_deployment(skip_build=True, no_docker=True)

    # Проверяем, что .env и все сертификаты успешно созданы
    assert (tmp_path / ".env").exists()
    certs_dir = tmp_path / "certs"
    assert (certs_dir / "ca.crt").exists()
    assert (certs_dir / "postgres.crt").exists()
    assert (certs_dir / "redis.crt").exists()
    assert (certs_dir / "minio.crt").exists()
    assert (certs_dir / "client.p12").exists()


def test_find_docker_compose_cmd():
    """Проверяет функцию поиска утилиты Docker Compose."""
    cmd = find_docker_compose_cmd()
    assert isinstance(cmd, list)
