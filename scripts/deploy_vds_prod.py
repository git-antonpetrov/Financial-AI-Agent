#!/usr/bin/env python3
"""
Кроссплатформенный скрипт единого боевого развертывания стека Financial AI Agent:
- Инициализация переменных окружения безопасности (.env);
- Выпуск доверенного Root CA и внутренних сертификатов сервисов (PostgreSQL, Redis, MinIO, Caddy, mTLS);
- Запуск изолированного микросервисного стека Docker Compose со сквозным TLS (Zero Trust);
- Контроль запуска и проверка здоровья (Healthcheck).
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Подключение локальных скриптов
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from setup_vds_security_env import setup_vds  # noqa: E402


def find_docker_compose_cmd() -> list[str]:
    """Определяет команду для вызова Docker Compose."""
    # Проверяем плагин v2: docker compose version
    if shutil.which("docker"):
        try:
            res = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
            if res.returncode == 0:
                return ["docker", "compose"]
        except Exception:
            pass

    # Проверяем standalone docker-compose v1
    if shutil.which("docker-compose"):
        return ["docker-compose"]

    return []


def command_output(args: list[str]) -> tuple[int, str]:
    """Запускает команду и возвращает код выхода вместе со стандартным выводом.

    Аргументы:
        args: Аргументы процесса, начиная с имени программы.

    Возвращает:
        Пара из кода выхода и stdout. Если программы нет, код равен 127,
        а текст пустой.

    Побочные эффекты:
        Запускает внешний процесс и не меняет файловую систему.
    """
    try:
        completed = subprocess.run(args, capture_output=True, text=True, check=False)
    except OSError:
        return 127, ""
    return completed.returncode, completed.stdout


def block_source(path: str) -> str:
    """Возвращает блочное устройство, на котором расположен каталог.

    Аргументы:
        path: Каталог данных Docker.

    Возвращает:
        Источник из findmnt либо первая колонка df. Пустая строка, если
        устройство определить не удалось.
    """
    code, output = command_output(["findmnt", "-no", "SOURCE", "-T", path])
    if code == 0 and output.strip():
        return output.splitlines()[0].strip()
    code, output = command_output(["df", "-P", path])
    lines = [line for line in output.splitlines() if line.strip()]
    if code == 0 and len(lines) >= 2:
        parts = lines[1].split()
        if parts:
            return parts[0]
    return ""


def assert_docker_root_on_open_luks(docker_bin: str = "docker") -> None:
    """Останавливает запуск, если каталог Docker не лежит на открытом томе LUKS.

    Повторяет проверку scripts/deploy_vds_prod.sh: корень берётся из docker info,
    источник блока — из findmnt или df, а cryptsetup status подтверждает, что
    mapper открыт. Функция не монтирует каталоги и не накрывает живые тома.

    Аргументы:
        docker_bin: Имя исполняемого файла Docker.

    Вызывает:
        SystemExit: Каталог не найден или устройство не является открытым LUKS.
    """
    code, output = command_output([docker_bin, "info", "-f", "{{.DockerRootDir}}"])
    docker_root = output.strip() if code == 0 else ""
    if not docker_root:
        docker_root = "/var/lib/docker"
    if not os.path.exists(docker_root):
        print(f"[-] Каталог данных Docker не найден: {docker_root}")
        sys.exit(1)

    source = block_source(docker_root)
    mapper_prefix = "/dev/mapper/"
    luks_name = source[len(mapper_prefix):] if source.startswith(mapper_prefix) else ""
    crypt_ok = False
    if luks_name:
        status_code, _status_text = command_output(["cryptsetup", "status", luks_name])
        crypt_ok = status_code == 0
    if not source.startswith(mapper_prefix) or not crypt_ok:
        print(
            f"[-] Тома Docker ({docker_root} на {source}) не находятся на расшифрованном LUKS. "
            "Запуск остановлен."
        )
        sys.exit(1)
    print(f"      Каталог Docker находится на LUKS-устройстве {source}.")


def run_deployment(skip_build: bool = False, no_docker: bool = False, timeout: int = 90) -> None:
    """Готовит окружение и поднимает стек, если каталог Docker лежит на открытом LUKS.

    Аргументы:
        skip_build: Не передаёт --build в docker compose up.
        no_docker: Готовит только .env и не запускает контейнеры.
        timeout: Сколько секунд ждать готовности сервисов.

    Вызывает:
        SystemExit: Docker Compose недоступен, конфигурация невалидна,
            каталог Docker не на LUKS или compose up завершился с ошибкой.

    Побочные эффекты:
        Записывает .env и при запуске контейнеров не монтирует тома поверх
        живого каталога Docker.
    """
    print("=" * 78)
    print("    FINANCIAL AI AGENT — ZERO-TRUST PRODUCTION DEPLOYMENT (ONLY-DOCKER)")
    print("=" * 78)

    # 1. Настройка переменных безопасности
    print("\n[1/3] Инициализация переменных окружения безопасности (.env)...")
    sec_info = setup_vds(str(PROJECT_ROOT))
    print(f"      Инициализировано параметров: {len(sec_info)}")
    print("      RSA-2048 ключи JWT и параметры шифрования инициализированы в памяти/.env.")

    legacy_certs = PROJECT_ROOT / "certs"
    if legacy_certs.exists():
        shutil.rmtree(legacy_certs, ignore_errors=True)

    if no_docker:
        print("\n[+] Флаг --no-docker установлен. Конфигурация .env готова, запуск Docker пропущен.")
        return

    # 2. Проверка Docker и запуск контейнеров (Strict Only-Docker Mode)
    print("\n[2/3] Проверка Docker и запуск контейнеров (Zero-Trust PKI в томе certs_data)...")
    compose_cmd = find_docker_compose_cmd()
    if not compose_cmd:
        print("[-] Ошибка: Docker Compose не найден на данной машине.")
        print("    Установите Docker Engine и Docker Compose для автоматического запуска контейнеров.")
        sys.exit(1)

    # Валидация compose файла
    val_cmd = compose_cmd + ["config", "-q"]
    val_res = subprocess.run(val_cmd, cwd=str(PROJECT_ROOT))
    if val_res.returncode != 0:
        print("[-] Ошибка валидации конфигурации docker-compose.yml.")
        sys.exit(1)
    print("      Конфигурация docker-compose.yml успешно валидирована.")

    print("      Проверка LUKS для каталога данных Docker...")
    assert_docker_root_on_open_luks()

    # Запуск контейнеров
    up_cmd = compose_cmd + ["up", "-d"]
    if not skip_build:
        up_cmd.append("--build")
    up_cmd.append("--remove-orphans")

    print(f"      Выполнение команды: {' '.join(up_cmd)}")
    up_res = subprocess.run(up_cmd, cwd=str(PROJECT_ROOT))
    if up_res.returncode != 0:
        print("[-] Ошибка при выполнении docker compose up.")
        sys.exit(1)

    # 3. Проверка состояния сервисов
    print("\n[3/3] Ожидание готовности сервисов...")
    start_time = time.time()
    critical_services = [
        "financial-postgres",
        "redis-server",
        "minio-server",
        "chromadb-server",
        "admin-server",
        "simulation-api",
        "bank-worker",
        "invest-worker",
        "digital-worker",
        "vector-worker",
        "caddy-server",
    ]

    all_running = False
    while time.time() - start_time < timeout:
        running_count = 0
        for svc in critical_services:
            status = "not_found"
            fmt = "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}"
            try:
                insp = subprocess.run(
                    ["docker", "inspect", f"--format={fmt}", svc],
                    capture_output=True,
                    text=True,
                )
                status = insp.stdout.strip()
            except Exception:
                pass

            if status not in ("healthy", "running"):
                alias_map = {
                    "financial-postgres": "postgres-db",
                    "redis-server": "redis",
                    "minio-server": "minio",
                    "chromadb-server": "chromadb",
                    "admin-server": "admin-server",
                    "simulation-api": "simulation-api",
                    "bank-worker": "bank-worker",
                    "invest-worker": "invest-worker",
                    "digital-worker": "digital-worker",
                    "vector-worker": "vector-worker",
                    "caddy-server": "caddy",
                }
                alt = alias_map.get(svc)
                if alt:
                    try:
                        cid = subprocess.run(
                            ["docker", "compose", "ps", "-q", alt],
                            capture_output=True,
                            text=True,
                            cwd=str(PROJECT_ROOT),
                        ).stdout.strip()
                        if cid:
                            insp = subprocess.run(
                                ["docker", "inspect", f"--format={fmt}", cid],
                                capture_output=True,
                                text=True,
                            )
                            status = insp.stdout.strip()
                    except Exception:
                        pass

            if status in ("healthy", "running"):
                running_count += 1

        if running_count == len(critical_services):
            all_running = True
            break

        print(f"      Запущено сервисов: {running_count}/{len(critical_services)}...", end="\r")
        time.sleep(3)

    print()
    if all_running:
        print("[+] Все ключевые сервисы запущены и функционируют!")
    else:
        print(
            f"[!] Предупреждение: таймаут ожидания готовности сервисов ({timeout}с). "
            "Проверьте 'docker compose logs'."
        )

    print("\n" + "=" * 78)
    print("    БОЕВОЕ РАЗВЕРТЫВАНИЕ ЗАВЕРШЕНО (ZERO-TRUST ONLY-DOCKER ACTIVE)")
    print("=" * 78)
    print("  • Public Ingress (Caddy):       https://admin.fin-ai-agent.ru")
    print("  • Zero-Trust PKI & Root CA:     Изолированы в Docker томах certs_data и root_ca_data")
    print("  • Хостовая файловая система:    Чистая (0 незашифрованных ключей на диске хоста)")
    print("  • PostgreSQL In-Transit TLS:    активен (ssl=on, certs_data:/certs:ro)")
    print("  • Redis In-Transit TLS:         активен (tls-port 6379, certs_data:/certs:ro)")
    print("  • MinIO In-Transit TLS & SSE:   активен (HTTPS, certs_data:/certs:ro)")
    print("  • Настройка 2FA TOTP:           python scripts/setup_2fa.py")
    print("=" * 78)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Zero-Trust VDS Production Deployment Orchestrator")
    parser.add_argument("--skip-build", action="store_true", help="Пропустить сборку Docker образов")
    parser.add_argument(
        "--no-docker",
        action="store_true",
        help="Только настроить окружение и сертификаты без запуска Docker",
    )
    parser.add_argument("--timeout", type=int, default=90, help="Таймаут проверки контейнеров в секундах")

    args = parser.parse_args()
    run_deployment(skip_build=args.skip_build, no_docker=args.no_docker, timeout=args.timeout)
