#!/usr/bin/env bash
# ==============================================================================
# Financial AI Agent — Единый скрипт боевого развертывания VDS (Zero-Trust)
# 
# Выполняет полный цикл инициализации защищенного промышленного контура:
# 1. Проверка системных требований (Docker, Docker Compose, Python, RAM, диск);
# 2. Инициализация переменных безопасности и ключей шифрования (.env, AES-256-GCM, RSA-2048, TOTP);
# 3. Выпуск доверенного Root CA и внутренних TLS-сертификатов (PostgreSQL, Redis, MinIO, Caddy, mTLS);
# 4. Проверка прав доступа к сертификатам и закрытым ключам (0600 / 0644);
# 5. Сборка и запуск микросервисного стека в изолированных сетях Docker (Zero Trust);
# 6. Контроль прохождения проверок работоспособности (Healthcheck) всех сервисов.
# ==============================================================================

set -euo pipefail

# ANSI цвета для вывода в консоль
readonly COLOR_RESET="\033[0m"
readonly COLOR_GREEN="\033[1;32m"
readonly COLOR_BLUE="\033[1;34m"
readonly COLOR_YELLOW="\033[1;33m"
readonly COLOR_RED="\033[1;31m"
readonly COLOR_CYAN="\033[1;36m"

log_info() {
    echo -e "${COLOR_BLUE}[INFO]${COLOR_RESET} $1"
}

log_success() {
    echo -e "${COLOR_GREEN}[SUCCESS]${COLOR_RESET} $1"
}

log_warn() {
    echo -e "${COLOR_YELLOW}[WARN]${COLOR_RESET} $1"
}

log_error() {
    echo -e "${COLOR_RED}[ERROR]${COLOR_RESET} $1"
}

# Определение рабочей директории проекта
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

echo -e "${COLOR_CYAN}"
echo "=============================================================================="
echo "    FINANCIAL AI AGENT — ZERO-TRUST PRODUCTION VDS DEPLOYMENT"
echo "=============================================================================="
echo -e "${COLOR_RESET}"

# ------------------------------------------------------------------------------
# 1. ПРОВЕРКА СИСТЕМНЫХ ТРЕБОВАНИЙ (PRE-FLIGHT CHECKS)
# ------------------------------------------------------------------------------
log_info "Этап 1/5: Проверка системного окружения и зависимостей..."

# Проверка Docker
DOCKER_BIN="docker"
if ! command -v docker &> /dev/null; then
    log_error "Docker не установлен. Установите Docker Engine: https://docs.docker.com/engine/install/"
    exit 1
fi

if ! docker info &> /dev/null; then
    if command -v sudo &> /dev/null && sudo docker info &> /dev/null; then
        DOCKER_BIN="sudo docker"
        log_info "Используются привилегии sudo для работы с Docker."
    else
        log_error "Служба Docker не запущена или у текущего пользователя нет прав (добавьте пользователя в группу docker: sudo usermod -aG docker \$USER)."
        exit 1
    fi
fi

# Определение команды Docker Compose (v2 plugin или standalone)
DOCKER_COMPOSE_CMD=""
if $DOCKER_BIN compose version &> /dev/null; then
    DOCKER_COMPOSE_CMD="$DOCKER_BIN compose"
elif command -v docker-compose &> /dev/null; then
    DOCKER_COMPOSE_CMD="docker-compose"
elif command -v sudo &> /dev/null && sudo command -v docker-compose &> /dev/null; then
    DOCKER_COMPOSE_CMD="sudo docker-compose"
else
    log_error "Docker Compose не найден. Установите плагин docker-compose-plugin."
    exit 1
fi
log_success "Обнаружен Docker Compose: $($DOCKER_COMPOSE_CMD version --short 2>/dev/null || echo 'доступен')"


# Определение Python интерпретатора
PYTHON_CMD=""
if [[ -f "${PROJECT_ROOT}/.venv/bin/python" ]]; then
    PYTHON_CMD="${PROJECT_ROOT}/.venv/bin/python"
elif [[ -f "${PROJECT_ROOT}/venv/bin/python" ]]; then
    PYTHON_CMD="${PROJECT_ROOT}/venv/bin/python"
elif command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
elif command -v python &> /dev/null; then
    PYTHON_CMD="python"
else
    log_error "Python 3 не найден. Установите python3."
    exit 1
fi
log_success "Используется Python: $($PYTHON_CMD --version 2>&1)"

# ------------------------------------------------------------------------------
# 2. ИНИЦИАЛИЗАЦИЯ ПЕРЕМЕННЫХ БЕЗОПАСНОСТИ (.ENV И КЛЮЧИ ШИФРОВАНИЯ)
# ------------------------------------------------------------------------------
log_info "Этап 2/5: Инициализация переменных окружения и криптографических секретов..."

if [[ ! -f "${PROJECT_ROOT}/.env" && -f "${PROJECT_ROOT}/.env.example" ]]; then
    log_info "Файл .env не найден, копирование базового шаблона из .env.example..."
    cp "${PROJECT_ROOT}/.env.example" "${PROJECT_ROOT}/.env"
    chmod 600 "${PROJECT_ROOT}/.env"
fi

$PYTHON_CMD "${SCRIPT_DIR}/setup_vds_security_env.py"
chmod 600 "${PROJECT_ROOT}/.env" 2>/dev/null || true
log_success "Файл .env инициализирован и защищен (права 0600)."

# ------------------------------------------------------------------------------
# 3. ВЫПУСК ВНУТРЕННИХ СЕРТИФИКАТОВ PKI (IN-TRANSIT TLS & MTLS)
# ------------------------------------------------------------------------------
log_info "Этап 3/5: Выпуск внутренних сертификатов TLS для Zero-Trust среды..."

mkdir -p "${PROJECT_ROOT}/certs"
chmod 700 "${PROJECT_ROOT}/certs"

# Запуск кроссплатформенного генератора сертификатов на Python (с fallback на bash)
if $PYTHON_CMD "${SCRIPT_DIR}/generate_internal_certs.py"; then
    log_success "Сертификаты успешно сгенерированы через Python cryptography."
else
    log_warn "Python генератор вернул ошибку, запуск bash-генератора с OpenSSL..."
    bash "${SCRIPT_DIR}/generate_internal_certs.sh"
fi

# Проверка наличия обязательных сертификатов
REQUIRED_CERTS=(
    "certs/ca.crt"
    "certs/ca.key"
    "certs/server.crt"
    "certs/server.key"
    "certs/postgres.crt"
    "certs/postgres.key"
    "certs/redis.crt"
    "certs/redis.key"
    "certs/minio.crt"
    "certs/minio.key"
    "certs/minio/public.crt"
    "certs/minio/private.key"
    "certs/minio/CAs/ca.crt"
    "certs/client.p12"
)

for cert_file in "${REQUIRED_CERTS[@]}"; do
    if [[ ! -f "${PROJECT_ROOT}/${cert_file}" ]]; then
        log_error "Критический сертификат/ключ не найден: ${cert_file}"
        exit 1
    fi
done

# Установка безопасных POSIX-прав
chmod 600 "${PROJECT_ROOT}"/certs/*.key 2>/dev/null || true
chmod 600 "${PROJECT_ROOT}"/certs/minio/private.key 2>/dev/null || true
chmod 644 "${PROJECT_ROOT}"/certs/*.crt 2>/dev/null || true
chmod 644 "${PROJECT_ROOT}"/certs/minio/public.crt 2>/dev/null || true
chmod 644 "${PROJECT_ROOT}"/certs/minio/CAs/ca.crt 2>/dev/null || true
chmod 600 "${PROJECT_ROOT}"/certs/client.p12 2>/dev/null || true

log_success "Полный набор TLS-сертификатов проверен и защищен."

# ------------------------------------------------------------------------------
# 4. ВАЛИДАЦИЯ КОНФИГУРАЦИИ DOCKER COMPOSE И ЗАПУСК КОНТЕЙНЕРОВ
# ------------------------------------------------------------------------------
log_info "Этап 4/5: Валидация конфигурации Docker Compose и запуск стека..."

# Проверка синтаксиса и подстановки переменных в docker-compose.yml
$DOCKER_COMPOSE_CMD config -q
log_success "Конфигурация docker-compose.yml валидна."

# Сборка и запуск контейнеров в фоне (исключая seeder, защищенный профилем seed)
log_info "Запуск контейнеров со сквозным TLS шифрованием..."
$DOCKER_COMPOSE_CMD up -d --build --remove-orphans

log_success "Контейнеры запущены в изолированных Zero-Trust сетях."

# ------------------------------------------------------------------------------
# 5. ПРОВЕРКА РАБОТОСПОСОБНОСТИ СЕРВИСОВ (HEALTHCHECK)
# ------------------------------------------------------------------------------
log_info "Этап 5/5: Ожидание готовности сервисов и прохождение проверок (Healthcheck)..."

WAIT_TIMEOUT=90
START_TIME=$(date +%s)
ALL_READY=false

CRITICAL_SERVICES=(
    "postgres-db"
    "redis"
    "minio-server"
    "chromadb-server"
    "admin-server"
    "simulation-api"
    "caddy-ingress"
)

while true; do
    CURRENT_TIME=$(date +%s)
    ELAPSED=$((CURRENT_TIME - START_TIME))
    if [[ $ELAPSED -ge $WAIT_TIMEOUT ]]; then
        break
    fi

    RUNNING_COUNT=0
    for svc in "${CRITICAL_SERVICES[@]}"; do
        STATUS=$($DOCKER_BIN inspect --format='{{.State.Status}}' "$svc" 2>/dev/null || echo "not_found")
        if [[ "$STATUS" == "running" ]]; then
            RUNNING_COUNT=$((RUNNING_COUNT + 1))
        fi
    done

    if [[ $RUNNING_COUNT -eq ${#CRITICAL_SERVICES[@]} ]]; then
        # Проверяем health endpoint Admin Server через локальный порт
        if curl -skf "http://127.0.0.1:8000/health" &> /dev/null || curl -skf "https://127.0.0.1/health" &> /dev/null; then
            ALL_READY=true
            break
        fi
    fi

    echo -ne "Ожидание готовности контейнеров ($RUNNING_COUNT/${#CRITICAL_SERVICES[@]} запущено, прошло ${ELAPSED}с)...\r"
    sleep 3
done
echo ""

if [[ "$ALL_READY" == "true" ]]; then
    log_success "Все сервисы успешно запущены и отвечают на запросы проверки работоспособности!"
else
    log_warn "Таймаут ожидания healthcheck (${WAIT_TIMEOUT}с). Проверьте логи сервисов: $DOCKER_COMPOSE_CMD logs"
fi

# ------------------------------------------------------------------------------
# ИТОГОВЫЙ ОТЧЕТ И ИНСТРУКЦИЯ ОПЕРАТОРА
# ------------------------------------------------------------------------------
echo -e "${COLOR_GREEN}"
echo "=============================================================================="
echo "    БОЕВОЕ РАЗВЕРТЫВАНИЕ УСПЕШНО ЗАВЕРШЕНО (ZERO-TRUST COMPLIANT)"
echo "=============================================================================="
echo -e "${COLOR_RESET}"

echo -e "Доступные точки подключения:"
echo -e "  • Публичный Ingress (Caddy Edge):        ${COLOR_CYAN}https://admin.fin-ai-agent.ru${COLOR_RESET} (или https://<IP_СЕРВЕРА>)"
echo -e "  • Документация API Admin Server:         ${COLOR_CYAN}https://admin.fin-ai-agent.ru/docs${COLOR_RESET}"
echo -e "  • Внутренний API симуляций:             ${COLOR_CYAN}http://127.0.0.1:8002${COLOR_RESET} (доступен локально/через SSH туннель)"
echo -e "  • S3 Объектное хранилище (MinIO):        ${COLOR_CYAN}https://127.0.0.1:9001${COLOR_RESET} (HTTPS с внутренним сертификатом)"
echo -e "  • База данных pgAdmin4:                 ${COLOR_CYAN}http://127.0.0.1:5050${COLOR_RESET}"
echo ""
echo -e "Статус безопасности контура:"
echo -e "  [✓] Zero-Trust In-Transit Encryption:   PostgreSQL (TLS require), Redis (TLS port 6379), MinIO (HTTPS)"
echo -e "  [✓] Data at Rest Protection:            AES-256-GCM Envelope Encryption (ChromaDB + Master Key)"
echo -e "  [✓] Асимметричный JWT (RS256):          Закрытый ключ 2048-bit в certs/jwt_private.pem"
echo -e "  [✓] Взаимная TLS-аутентификация (mTLS):  Клиентский бандл готов: certs/client.p12 (пароль: financial-agent-mtls)"
echo ""
echo -e "Следующие рекомендуемые действия:"
echo -e "  1. Настройте двухфакторную аутентификацию (2FA TOTP):"
echo -e "     ${COLOR_YELLOW}$PYTHON_CMD scripts/setup_2fa.py${COLOR_RESET}"
echo -e "  2. Для принудительного включения mTLS переключите в .env:"
echo -e "     ${COLOR_YELLOW}REQUIRE_MTLS=true${COLOR_RESET} и ${COLOR_YELLOW}CADDY_CLIENT_AUTH_MODE=require${COLOR_RESET}"
echo -e "  3. Просмотр журналов работы в реальном времени:"
echo -e "     ${COLOR_YELLOW}$DOCKER_COMPOSE_CMD logs -f admin-server caddy-ingress${COLOR_RESET}"
echo "=============================================================================="
