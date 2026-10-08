import os
import shutil
from pathlib import Path
import pytest

from scripts.setup_vds_security_env import setup_vds
from scripts.deploy_vds_prod import find_docker_compose_cmd, run_deployment


def test_setup_vds_security_env_creates_and_hardens_env(tmp_path):
    """
    Проверяет корректность инициализации переменных окружения и ключей:
    - генерация RSA пары для JWT подписи в оперативной памяти и запись в .env;
    - отсутствие утечки незашифрованных ключей в certs/ на хосте;
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

    # 1. Проверяем генерацию RSA ключей в .env и отсутствие создания certs/ на хосте
    assert not (tmp_path / "certs").exists(), "Папка certs/ не должна создаваться на хосте (Strict Only-Docker Mode)"
    env_file = tmp_path / ".env"
    assert env_file.exists(), ".env должен быть создан"
    env_content = env_file.read_text(encoding="utf-8")
    assert "JWT_PRIVATE_KEY=" in env_content
    assert "JWT_PUBLIC_KEY=" in env_content
    assert "-----BEGIN PRIVATE KEY-----" in env_content
    assert "-----BEGIN PUBLIC KEY-----" in env_content

    # 2. Zero-Trust TLS параметры
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
    - вызов шагов: setup_vds_security_env.py, docker compose up -d;
    - валидация томов Zero-Trust PKI и контроль healthcheck.
    """
    root_dir = Path(__file__).resolve().parent.parent
    sh_path = root_dir / "scripts" / "deploy_vds_prod.sh"
    assert sh_path.exists(), "scripts/deploy_vds_prod.sh должен присутствовать"

    content = sh_path.read_text(encoding="utf-8")
    assert "#!/usr/bin/env bash" in content
    assert "set -euo pipefail" in content
    assert "setup_vds_security_env.py" in content
    assert "up -d --build" in content
    assert "DOCKER_COMPOSE_CMD" in content
    assert "certs_data" in content
    assert "admin-server" in content
    assert "caddy-ingress" in content


def test_deploy_vds_prod_python_orchestrator_no_docker(tmp_path, monkeypatch):
    """
    Проверяет выполнение сценария развертывания через Python скрипт deploy_vds_prod.py
    в режиме --no-docker без запуска контейнеров (Strict Only-Docker Mode).
    """
    root_dir = Path(__file__).resolve().parent.parent
    example_env = root_dir / ".env.example"
    shutil.copy(example_env, tmp_path / ".env.example")

    import scripts.deploy_vds_prod as deploy_mod
    monkeypatch.setattr(deploy_mod, "PROJECT_ROOT", tmp_path)

    # Запуск этапов подготовки без вызова Docker
    deploy_mod.run_deployment(skip_build=True, no_docker=True)

    # Проверяем, что .env успешно создан и настроен, а папка certs на хосте отсутствует
    assert (tmp_path / ".env").exists()
    assert not (tmp_path / "certs").exists(), "Хостовая папка certs/ не должна существовать"


def test_find_docker_compose_cmd():
    """Проверяет функцию поиска утилиты Docker Compose."""
    cmd = find_docker_compose_cmd()
    assert isinstance(cmd, list)
