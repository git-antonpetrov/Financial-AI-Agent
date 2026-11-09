#!/usr/bin/env python3
"""Поднимает боевой контур на VDS после того, как проект уже лежит на открытом LUKS.

Секреты пишет в .env и не печатает. Сертификаты PKI выпускает контейнер root-ca,
а не хостовый каталог certs.
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
    # Проверяет плагин v2: docker compose version
    if shutil.which("docker"):
        try:
            res = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
            if res.returncode == 0:
                return ["docker", "compose"]
        except Exception:
            pass

    # Проверяет standalone docker-compose v1
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


def evaluate_luks_status(status_text: str) -> tuple[bool, str]:
    """Проверяет, что вывод cryptsetup status описывает LUKS aes-xts от 512 бит.

    Имя mapper в тексте не сравнивается с константой: важен открытый LUKS и шифр.

    Аргументы:
        status_text: Стандартный вывод ``cryptsetup status``.

    Возвращает:
        Пара из признака соответствия и причины отказа. При успехе причина пустая.
    """
    cipher = ""
    device_type = ""
    key_bits = 0
    for raw_line in status_text.splitlines():
        line = raw_line.strip().lower()
        if ":" not in line:
            continue
        label, raw_value = line.split(":", 1)
        value = raw_value.strip()
        if label == "type":
            device_type = value
        elif label == "cipher":
            cipher = value
        elif label == "keysize":
            digits = "".join(character for character in value if character.isdigit())
            key_bits = int(digits) if digits else 0
    if "luks" not in device_type:
        return False, f"устройство не является LUKS (type={device_type or 'unknown'})"
    if not cipher.startswith("aes-xts") or key_bits < 512:
        return False, (
            "шифр должен быть aes-xts с ключом не меньше 512 бит "
            f"(cipher={cipher or 'unknown'}, keysize={key_bits})"
        )
    return True, ""


def assert_docker_root_on_open_luks(docker_bin: str = "docker") -> str:
    """Останавливает запуск, если каталог Docker не лежит на открытом LUKS aes-xts.

    Корень берётся из docker info, источник блока — из findmnt или df.
    cryptsetup status подтверждает, что mapper открыт, тип — LUKS, шифр — aes-xts,
    а ключ не короче 512 бит. Имя mapper не зафиксировано. Функция не монтирует
    каталоги и не накрывает живые тома.

    Аргументы:
        docker_bin: Имя исполняемого файла Docker.

    Возвращает:
        Точку монтирования каталога Docker. Пустую строку, если findmnt её не назвал.

    Вызывает:
        SystemExit: Каталог не найден, устройство не LUKS или шифр слабее политики.
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
    if not source.startswith(mapper_prefix) or not luks_name:
        print(
            f"[-] Тома Docker ({docker_root} на {source}) не находятся на расшифрованном LUKS. "
            "Запуск остановлен."
        )
        sys.exit(1)

    status_code, status_text = command_output(["cryptsetup", "status", luks_name])
    if status_code != 0 or not status_text.strip():
        print(
            f"[-] Тома Docker ({docker_root} на {source}) не находятся на расшифрованном LUKS. "
            "Запуск остановлен."
        )
        sys.exit(1)
    accepted, reason = evaluate_luks_status(status_text)
    if not accepted:
        print(f"[-] LUKS {source} отклонён: {reason}. Запуск остановлен.")
        print("    Подготовка хоста: sudo bash scripts/host_bootstrap.sh")
        sys.exit(1)
    print(f"      Каталог Docker находится на LUKS-устройстве {source}.")
    return mount_target(docker_root)


def mount_target(path: str) -> str:
    """Возвращает точку монтирования каталога.

    Аргументы:
        path: Файл или каталог, который нужно соотнести с томом.

    Возвращает:
        Точку монтирования из findmnt. Пустую строку, если утилита не ответила.
    """
    code, output = command_output(["findmnt", "-no", "TARGET", "-T", path])
    if code == 0 and output.strip():
        return output.splitlines()[0].strip()
    return ""


def assert_project_on_mount(project: str, mount: str) -> None:
    """Останавливает запуск, если каталог проекта лежит не на том же LUKS-томе.

    Аргументы:
        project: Абсолютный путь к корню репозитория.
        mount: Точка монтирования открытого тома, где находится каталог Docker.

    Вызывает:
        SystemExit: Точка монтирования проекта не совпадает с томом Docker
            или findmnt её не определил.

    Побочные эффекты:
        Ничего не записывает. Секреты остаются на зашифрованном томе.
    """
    project_mount = mount_target(project)
    if not mount or not project_mount:
        print("[-] findmnt не определил точку монтирования проекта или Docker. Запуск остановлен.")
        print("    Подготовка хоста: sudo bash scripts/host_bootstrap.sh")
        sys.exit(1)
    if Path(project_mount).resolve() != Path(mount).resolve():
        print(
            f"[-] Каталог проекта {project} смонтирован на {project_mount}, "
            f"а данные Docker — на {mount}."
        )
        print("    Клон, .env и data/ должны лежать на открытом LUKS-томе.")
        print("    Подготовка хоста: sudo bash scripts/host_bootstrap.sh")
        sys.exit(1)


def verify_host_private_key_modes(cert_dir: Path) -> None:
    """Проверяет режим 0600 у закрытых ключей, если каталог сертификатов уже есть.

    Отсутствующий каталог остаётся штатным стартом до bootstrap и не останавливает запуск.

    Аргументы:
        cert_dir: Каталог хостовых сертификатов.

    Вызывает:
        SystemExit: Найден закрытый ключ с режимом, отличным от 0600.
    """
    if not cert_dir.is_dir():
        print(f"Каталог сертификатов {cert_dir} ещё не создан. Проверка режима 0600 пропущена до bootstrap.")
        return
    keys = [path for path in cert_dir.rglob("*.key") if path.is_file()]
    if not keys:
        print(f"В {cert_dir} нет закрытых ключей. Проверка режима 0600 нечего подтверждать.")
        return
    for key_file in keys:
        mode = key_file.stat().st_mode & 0o777
        if mode != 0o600:
            print(f"[-] Закрытый ключ {key_file} имеет режим {mode:o}, требуется 0600.")
            sys.exit(1)
    print(f"Закрытые ключи в {cert_dir} имеют режим 0600.")


def remove_legacy_host_certs(project: Path) -> None:
    """Удаляет устаревший хостовый каталог certs после проверки режима ключей.

    Аргументы:
        project: Корень репозитория.

    Побочные эффекты:
        Удаляет project/certs, если каталог существует. Ключи PKI остаются в томах Docker.
    """
    verify_host_private_key_modes(project / "certs")
    legacy = project / "certs"
    if legacy.exists():
        shutil.rmtree(legacy, ignore_errors=True)
        print("      Устаревший хостовый каталог certs/ удалён. Сертификаты выпускает контейнер root-ca.")


def require_gcp_service_account(project: Path) -> None:
    """Проверяет, что ключ GCP лежит в data/ и не доступен группе или остальным.

    Аргументы:
        project: Корень репозитория на открытом LUKS-томе.

    Вызывает:
        SystemExit: Файл отсутствует, не является обычным файлом или доступен шире владельца.
    """
    key_file = project / "data" / "gcp-service-account.json"
    if not key_file.is_file():
        print(f"[-] Нет ключа GCP: {key_file}")
        print("    Положите файл учётной записи на зашифрованный том и выставьте режим 0600.")
        sys.exit(1)
    mode = key_file.stat().st_mode & 0o777
    if mode & 0o077:
        print(f"[-] Ключ GCP {key_file} имеет режим {mode:o}. Требуется доступ только владельца, например 0600.")
        sys.exit(1)


def container_is_ready(raw_status: str) -> bool:
    """Сообщает, можно ли считать контейнер готовым.

    Контейнер с healthcheck готов только в состоянии healthy.
    Контейнер без healthcheck готов только в состоянии running.

    Аргументы:
        raw_status: Строка inspect вида health:healthy или state:running.

    Возвращает:
        True, если состояние допускает приём трафика или штатную работу воркера.
    """
    text = raw_status.strip()
    if text.startswith("health:"):
        return text.split(":", 1)[1] == "healthy"
    if text.startswith("state:"):
        return text.split(":", 1)[1] == "running"
    return False


def deployment_succeeded(service_status: dict[str, str], edge_ok: bool) -> bool:
    """Сообщает, что все обязательные сервисы и проверка Caddy /health успешны.

    Аргументы:
        service_status: Имя сервиса и его строка inspect.
        edge_ok: True, если wget внутри контейнера Caddy получил ответ /health.

    Возвращает:
        True, если каждый сервис готов и граница отвечает.
    """
    if not edge_ok:
        return False
    return all(container_is_ready(status) for status in service_status.values())


INSPECT_FORMAT = "{{if .State.Health}}health:{{.State.Health.Status}}{{else}}state:{{.State.Status}}{{end}}"
CRITICAL_SERVICES = (
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
)
SERVICE_ALIASES = {
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
    "caddy-ingress": "caddy",
}


def inspect_status(docker_bin: str, name: str) -> str:
    """Читает состояние контейнера и не подменяет healthcheck обычным running.

    Аргументы:
        docker_bin: Исполняемый файл Docker.
        name: Имя или идентификатор контейнера.

    Возвращает:
        Строку health:* или state:*. Пустую строку, если контейнер не найден.
    """
    completed = subprocess.run(
        [docker_bin, "inspect", f"--format={INSPECT_FORMAT}", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def edge_health_ok(docker_bin: str) -> bool:
    """Проверяет /health на loopback внутри Caddy, а не на опубликованном порту хоста.

    Аргументы:
        docker_bin: Исполняемый файл Docker.

    Возвращает:
        True, если процесс внутри контейнера ответил OK.
    """
    completed = subprocess.run(
        [docker_bin, "exec", "caddy-server", "wget", "-qO-", "http://127.0.0.1/health"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and "OK" in completed.stdout


def read_env_value(env_path: Path, key: str, default: str) -> str:
    """Читает одно значение из .env и не печатает остальные секреты.

    Аргументы:
        env_path: Файл окружения.
        key: Имя переменной.
        default: Значение, если ключ отсутствует.

    Возвращает:
        Значение ключа без кавычек либо default.
    """
    if not env_path.is_file():
        return default
    prefix = f"{key}="
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip().strip('"').strip("'")
    return default


def collect_service_status(docker_bin: str, compose_cmd: list[str]) -> dict[str, str]:
    """Собирает состояние обязательных контейнеров.

    Аргументы:
        docker_bin: Исполняемый файл Docker.
        compose_cmd: Команда Docker Compose, которой ищутся идентификаторы.

    Возвращает:
        Словарь из имени контейнера и строки inspect.
    """
    report: dict[str, str] = {}
    for service in CRITICAL_SERVICES:
        status = inspect_status(docker_bin, service)
        if not container_is_ready(status):
            alias = SERVICE_ALIASES.get(service, "")
            if alias:
                completed = subprocess.run(
                    compose_cmd + ["ps", "-q", alias],
                    capture_output=True,
                    text=True,
                    cwd=str(PROJECT_ROOT),
                    check=False,
                )
                container_id = completed.stdout.strip()
                if container_id:
                    status = inspect_status(docker_bin, container_id)
        report[service] = status
    return report


def run_deployment(skip_build: bool = False, no_docker: bool = False, timeout: int = 180) -> None:
    """Готовит окружение и поднимает стек, если проект и Docker лежат на одном открытом LUKS.

    Пароль администратора и ключи не печатает. Сертификаты PKI выпускает контейнер root-ca.

    Аргументы:
        skip_build: Не передаёт --build в docker compose up.
        no_docker: Готовит только .env и не проверяет LUKS.
        timeout: Сколько секунд ждать готовности сервисов.

    Вызывает:
        SystemExit: Docker Compose недоступен, конфигурация невалидна,
            каталог Docker или проект не на LUKS, нет ключа GCP,
            compose up завершился с ошибкой или сервис не стал готов.

    Побочные эффекты:
        Записывает .env на диске проекта. Боевой запуск допускается только когда
        этот диск уже является открытым LUKS-томом.
    """
    print("=" * 78)
    print("    FINANCIAL AI AGENT — РАЗВЁРТЫВАНИЕ НА VDS")
    print("=" * 78)

    if not no_docker:
        print("\n[1/4] Проверка LUKS для каталога Docker и каталога проекта...")
        mount = assert_docker_root_on_open_luks()
        assert_project_on_mount(str(PROJECT_ROOT.resolve()), mount)
    else:
        print("\n[1/4] Режим --no-docker: проверка LUKS пропущена.")

    print("\n[2/4] Секреты окружения и устаревшие хостовые ключи...")
    remove_legacy_host_certs(PROJECT_ROOT)
    sec_info = setup_vds(str(PROJECT_ROOT))
    print(f"      Инициализировано параметров: {len(sec_info)}")
    if no_docker:
        print("\n[+] Флаг --no-docker установлен. Конфигурация .env готова, запуск Docker пропущен.")
        return

    print("\n[3/4] Проверка ключа GCP и запуск контейнеров...")
    require_gcp_service_account(PROJECT_ROOT)
    compose_cmd = find_docker_compose_cmd()
    if not compose_cmd:
        print("[-] Docker Compose не найден.")
        print("    Подготовка хоста: sudo bash scripts/host_bootstrap.sh")
        sys.exit(1)
    docker_bin = "docker"

    validation = subprocess.run(compose_cmd + ["config", "-q"], cwd=str(PROJECT_ROOT), check=False)
    if validation.returncode != 0:
        print("[-] docker compose config не принял конфигурацию.")
        sys.exit(1)
    print("      Конфигурация docker-compose.yml принята.")

    up_cmd = compose_cmd + ["up", "-d"]
    if not skip_build:
        up_cmd.append("--build")
    up_cmd.append("--remove-orphans")
    print(f"      Выполнение команды: {' '.join(up_cmd)}")
    started = subprocess.run(up_cmd, cwd=str(PROJECT_ROOT), check=False)
    if started.returncode != 0:
        print("[-] docker compose up завершился с ошибкой.")
        sys.exit(1)

    print("\n[4/4] Ожидание healthcheck и ответа Caddy /health внутри контейнера...")
    deadline = time.monotonic() + timeout
    report = {service: "" for service in CRITICAL_SERVICES}
    edge_ok = False
    while time.monotonic() < deadline:
        report = collect_service_status(docker_bin, compose_cmd)
        edge_ok = edge_health_ok(docker_bin)
        ready = sum(1 for status in report.values() if container_is_ready(status))
        if deployment_succeeded(report, edge_ok):
            break
        print(f"      Готово сервисов: {ready}/{len(CRITICAL_SERVICES)}, граница Caddy: {edge_ok}", end="\r")
        time.sleep(3)
    print()

    if not deployment_succeeded(report, edge_ok):
        pending = [name for name, status in report.items() if not container_is_ready(status)]
        print(f"[-] Сервисы не стали готовы за {timeout} с: {', '.join(pending) or 'граница Caddy'}.")
        print(f"    Журнал: {' '.join(compose_cmd)} logs")
        sys.exit(1)

    domain = read_env_value(PROJECT_ROOT / ".env", "ADMIN_DOMAIN", "admin.fin-ai-agent.ru")
    print("\n" + "=" * 78)
    print("    РАЗВЁРТЫВАНИЕ ЗАВЕРШЕНО")
    print("=" * 78)
    print(f"  • Вход администратора:          https://{domain}")
    print("  • Сертификаты PKI:              тома Docker certs_data и root_ca_data")
    print("  • Секреты:                      .env и data/ на открытом LUKS-томе, в git не входят")
    print("  • Двухфакторная аутентификация: .venv/bin/python scripts/setup_2fa.py")
    print(f"  • Журнал:                       {' '.join(compose_cmd)} logs -f admin-server caddy")
    print("  • После перезагрузки сервера:   sudo bash scripts/unlock_volumes.sh")
    print("=" * 78)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Zero-Trust VDS Production Deployment Orchestrator")
    parser.add_argument("--skip-build", action="store_true", help="Пропустить сборку Docker образов")
    parser.add_argument(
        "--no-docker",
        action="store_true",
        help="Только настроить окружение и сертификаты без запуска Docker",
    )
    parser.add_argument("--timeout", type=int, default=180, help="Таймаут проверки контейнеров в секундах")

    args = parser.parse_args()
    run_deployment(skip_build=args.skip_build, no_docker=args.no_docker, timeout=args.timeout)
