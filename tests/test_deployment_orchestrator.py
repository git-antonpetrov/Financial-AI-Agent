import shutil
from pathlib import Path

import pytest

from scripts.setup_vds_security_env import setup_vds
from scripts.deploy_vds_prod import find_docker_compose_cmd


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
    assert "POSTGRES_SSLMODE=verify-full" in env_content
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
    assert "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=" not in env_content
    assert "ALLOWED_MTLS_SUBJECTS=superadmin-workstation" in env_content
    assert "bootstrap_token_bank_agent_secret" not in env_content
    assert "minio_secret_key_placeholder" not in env_content


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
    assert "aes-xts" in content
    assert "512" in content
    assert "encrypted_docker_volumes" not in content
    assert "verify_host_private_key_modes" in content
    assert "ещё не создан" in content


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


def _env_value(content: str, key: str) -> str:
    """Возвращает значение ключа из текста .env."""
    prefix = f"{key}="
    for line in content.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    raise AssertionError(f"Ключ {key} отсутствует")


def test_setup_keeps_random_secrets_and_fills_empty_mtls_subjects(tmp_path):
    """Проверяет замену известных плейсхолдеров и сохранение уже случайных секретов."""
    import base64

    good_kms = "financial-kms-key:" + base64.b64encode(b"R" * 32).decode("ascii")
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                f"MINIO_KMS_SECRET_KEY={good_kms}",
                "ALLOWED_MTLS_SUBJECTS=",
                "POSTGRES_PASSWORD=change_this_strong_password_in_production",
                "REDIS_PASSWORD=already-random-redis-password-value",
                "DATA_ENCRYPTION_KEY=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                "ADMIN_TOTP_SECRET=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
                "MINIO_ACCESS_KEY=minio_access_key_placeholder",
                "MINIO_SECRET_KEY=minio_secret_key_placeholder",
                "AGENT_BANK_BOOTSTRAP_TOKEN=bootstrap_token_bank_agent_secret",
                'JWT_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----"',
                'JWT_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----\\nabc\\n-----END PUBLIC KEY-----"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    setup_vds(str(tmp_path))
    first = (tmp_path / ".env").read_text(encoding="utf-8")
    assert f"MINIO_KMS_SECRET_KEY={good_kms}" in first
    assert "ALLOWED_MTLS_SUBJECTS=superadmin-workstation" in first
    assert "change_this_strong_password_in_production" not in first
    assert "REDIS_PASSWORD=already-random-redis-password-value" in first
    assert "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" not in first
    assert "JBSWY3DPEHPK3PXP" not in first
    assert "minio_access_key_placeholder" not in first
    assert "minio_secret_key_placeholder" not in first
    assert "bootstrap_token_bank_agent_secret" not in first
    postgres_password = _env_value(first, "POSTGRES_PASSWORD")
    encryption_key = _env_value(first, "DATA_ENCRYPTION_KEY")

    setup_vds(str(tmp_path))
    second = (tmp_path / ".env").read_text(encoding="utf-8")
    assert f"MINIO_KMS_SECRET_KEY={good_kms}" in second
    assert _env_value(second, "POSTGRES_PASSWORD") == postgres_password
    assert _env_value(second, "DATA_ENCRYPTION_KEY") == encryption_key
    assert "REDIS_PASSWORD=already-random-redis-password-value" in second


def test_allowed_mtls_subjects_use_issued_client_cn(tmp_path):
    """Проверяет, что пустой список субъектов берёт CN из выпущенного клиентского сертификата."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    import datetime

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "desk-workstation")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(7)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    certs = tmp_path / "certs"
    certs.mkdir()
    (certs / "admin_client.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "ALLOWED_MTLS_SUBJECTS=",
                "MINIO_KMS_SECRET_KEY=financial-kms-key:" + __import__("base64").b64encode(b"Q" * 32).decode("ascii"),
                'JWT_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----"',
                'JWT_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----\\nabc\\n-----END PUBLIC KEY-----"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    setup_vds(str(tmp_path))
    content = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "ALLOWED_MTLS_SUBJECTS=desk-workstation" in content


def test_custom_mtls_subjects_stay_untouched(tmp_path):
    """Проверяет, что непустой список субъектов не затирается."""
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "ALLOWED_MTLS_SUBJECTS=custom-operator",
                'JWT_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----"',
                'JWT_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----\\nabc\\n-----END PUBLIC KEY-----"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    setup_vds(str(tmp_path))
    content = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "ALLOWED_MTLS_SUBJECTS=custom-operator" in content
    assert "ALLOWED_MTLS_SUBJECTS=superadmin-workstation" not in content


def test_luks_gate_rejects_plain_disk(monkeypatch):
    """Проверяет отказ запуска, если корень Docker лежит не на mapper LUKS."""
    import scripts.deploy_vds_prod as deploy_mod

    def fake_command(args):
        if args[0] == "docker":
            return 0, "/var/lib/docker\n"
        if args[0] == "findmnt":
            return 0, "/dev/sda1\n"
        return 1, ""

    original_exists = deploy_mod.os.path.exists

    def fake_exists(path):
        if path == "/var/lib/docker":
            return True
        return original_exists(path)

    monkeypatch.setattr(deploy_mod, "command_output", fake_command)
    monkeypatch.setattr(deploy_mod.os.path, "exists", fake_exists)
    with pytest.raises(SystemExit) as exc:
        deploy_mod.assert_docker_root_on_open_luks()
    assert exc.value.code == 1


def test_luks_gate_accepts_open_mapper(monkeypatch):
    """Проверяет пропуск запуска, когда cryptsetup видит открытый LUKS."""
    import scripts.deploy_vds_prod as deploy_mod

    def fake_command(args):
        if args[0] == "docker":
            return 0, "/var/lib/docker\n"
        if args[0] == "findmnt":
            return 1, ""
        if args[0] == "df":
            return 0, "Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/mapper/docker_crypt 1 1 1 1% /\n"
        if args[0] == "cryptsetup":
            assert args == ["cryptsetup", "status", "docker_crypt"]
            assert args[2] != "encrypted_docker_volumes"
            return 0, (
                "/dev/mapper/docker_crypt is active.\n"
                "  type:    LUKS2\n"
                "  cipher:  aes-xts-plain64\n"
                "  keysize: 512 bits\n"
            )
        return 1, ""

    original_exists = deploy_mod.os.path.exists

    def fake_exists(path):
        if path == "/var/lib/docker":
            return True
        return original_exists(path)

    monkeypatch.setattr(deploy_mod, "command_output", fake_command)
    monkeypatch.setattr(deploy_mod.os.path, "exists", fake_exists)
    deploy_mod.assert_docker_root_on_open_luks()


def test_luks_gate_accepts_other_mapper_name(monkeypatch):
    """Проверяет, что политика смотрит на шифр, а не на имя encrypted_docker_volumes."""
    import scripts.deploy_vds_prod as deploy_mod

    def fake_command(args):
        if args[0] == "docker":
            return 0, "/var/lib/docker\n"
        if args[0] == "findmnt":
            return 0, "/dev/mapper/dm_fin_agent_secure_data\n"
        if args[0] == "cryptsetup":
            assert args[2] == "dm_fin_agent_secure_data"
            return 0, (
                "/dev/mapper/dm_fin_agent_secure_data is active.\n"
                "  type:    LUKS2\n"
                "  cipher:  aes-xts-plain64\n"
                "  keysize: 512 bits\n"
            )
        return 1, ""

    original_exists = deploy_mod.os.path.exists

    def fake_exists(path):
        if path == "/var/lib/docker":
            return True
        return original_exists(path)

    monkeypatch.setattr(deploy_mod, "command_output", fake_command)
    monkeypatch.setattr(deploy_mod.os.path, "exists", fake_exists)
    deploy_mod.assert_docker_root_on_open_luks()


def test_luks_gate_rejects_weak_cipher(monkeypatch):
    """Проверяет отказ, если открытый LUKS использует не aes-xts или ключ короче 512 бит."""
    import scripts.deploy_vds_prod as deploy_mod

    def fake_command(args):
        if args[0] == "docker":
            return 0, "/var/lib/docker\n"
        if args[0] == "findmnt":
            return 0, "/dev/mapper/docker_crypt\n"
        if args[0] == "cryptsetup":
            return 0, (
                "/dev/mapper/docker_crypt is active.\n"
                "  type:    LUKS2\n"
                "  cipher:  aes-xts-plain64\n"
                "  keysize: 256 bits\n"
            )
        return 1, ""

    original_exists = deploy_mod.os.path.exists

    def fake_exists(path):
        if path == "/var/lib/docker":
            return True
        return original_exists(path)

    monkeypatch.setattr(deploy_mod, "command_output", fake_command)
    monkeypatch.setattr(deploy_mod.os.path, "exists", fake_exists)
    with pytest.raises(SystemExit) as exc:
        deploy_mod.assert_docker_root_on_open_luks()
    assert exc.value.code == 1


def test_luks_status_rejects_non_luks_and_non_xts():
    """Проверяет разбор текста cryptsetup без привязки к имени mapper."""
    from scripts.deploy_vds_prod import evaluate_luks_status

    accepted, reason = evaluate_luks_status(
        "type: LUKS1\ncipher: aes-xts-plain\nkeysize: 512 bits\n"
    )
    assert accepted is True
    assert reason == ""

    plain_ok, plain_reason = evaluate_luks_status(
        "type: PLAIN\ncipher: aes-xts-plain64\nkeysize: 512 bits\n"
    )
    assert plain_ok is False
    assert "LUKS" in plain_reason

    cbc_ok, cbc_reason = evaluate_luks_status(
        "type: LUKS2\ncipher: aes-cbc-essiv:sha256\nkeysize: 256 bits\n"
    )
    assert cbc_ok is False
    assert "aes-xts" in cbc_reason


def test_encrypted_volume_script_refuses_live_docker_and_prints_move():
    """Проверяет запрет монтирования поверх живого Docker и команду переноса data-root."""
    root_dir = Path(__file__).resolve().parent.parent
    content = (root_dir / "scripts" / "setup_encrypted_volumes.sh").read_text(encoding="utf-8")
    assert "docker_daemon_is_running" in content
    assert "нельзя монтировать том поверх живого каталога Docker" in content
    assert "rsync -aHAX /var/lib/docker/" in content
    assert "data-root" in content
    assert "encrypted_docker_volumes" not in content


def test_luks_gate_rejects_closed_mapper(monkeypatch):
    """Проверяет отказ, если mapper есть, но cryptsetup не подтверждает открытый том."""
    import scripts.deploy_vds_prod as deploy_mod

    def fake_command(args):
        if args[0] == "docker":
            return 0, "/mnt/docker\n"
        if args[0] == "findmnt":
            return 0, "/dev/mapper/docker_crypt\n"
        if args[0] == "cryptsetup":
            return 1, ""
        return 1, ""

    original_exists = deploy_mod.os.path.exists

    def fake_exists(path):
        if path == "/mnt/docker":
            return True
        return original_exists(path)

    monkeypatch.setattr(deploy_mod, "command_output", fake_command)
    monkeypatch.setattr(deploy_mod.os.path, "exists", fake_exists)
    with pytest.raises(SystemExit):
        deploy_mod.assert_docker_root_on_open_luks()


def test_run_deployment_stops_before_compose_up_without_luks(tmp_path, monkeypatch):
    """Проверяет, что docker compose up не стартует без открытого LUKS."""
    root_dir = Path(__file__).resolve().parent.parent
    shutil.copy(root_dir / ".env.example", tmp_path / ".env.example")
    import scripts.deploy_vds_prod as deploy_mod

    monkeypatch.setattr(deploy_mod, "PROJECT_ROOT", tmp_path)

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and "up" in cmd:
            raise AssertionError("compose up не должен стартовать")

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    original_exists = deploy_mod.os.path.exists

    def fake_exists(path):
        if path == "/var/lib/docker":
            return False
        return original_exists(path)

    monkeypatch.setattr(deploy_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(deploy_mod.os.path, "exists", fake_exists)
    with pytest.raises(SystemExit) as exc:
        deploy_mod.run_deployment(skip_build=True, no_docker=False)
    assert exc.value.code == 1
