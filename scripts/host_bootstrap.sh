#!/usr/bin/env bash
# Ставит пакеты хоста, создаёт LUKS-том и переносит клон на этот том.
# Запускается один раз из интерактивной сессии root и не вызывается из GitHub Action.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=lib/paths.sh
source "${SCRIPT_DIR}/lib/paths.sh"

if [[ "${EUID}" -ne 0 ]]; then
    echo "[-] Скрипт запускается только от root." >&2
    exit 1
fi
if [[ ! -t 0 ]]; then
    echo "[-] Нужна интерактивная сессия оператора." >&2
    exit 1
fi

if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y \
        ca-certificates \
        cryptsetup \
        e2fsprogs \
        git \
        python3 \
        python3-pip \
        python3-venv \
        rsync \
        util-linux
    if ! command -v docker >/dev/null 2>&1; then
        apt-get install -y docker.io
    fi
    if ! docker compose version >/dev/null 2>&1; then
        apt-get install -y docker-compose-v2 || apt-get install -y docker-compose-plugin
    fi
else
    echo "[-] Поддерживается только хост с apt-get." >&2
    exit 1
fi

groupadd -f "${FIN_AGENT_GROUP}"
if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    usermod -aG "${FIN_AGENT_GROUP}" "${SUDO_USER}"
    usermod -aG docker "${SUDO_USER}"
fi

if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet docker; then
    echo "[+] Останавливает Docker перед переносом data-root."
    systemctl stop docker
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if ! pgrep -x dockerd >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
fi

bash "${SCRIPT_DIR}/setup_encrypted_volumes.sh"

mkdir -p "${FIN_AGENT_APP_DIR}"
source_root="$(realpath "${PROJECT_ROOT}")"
app_root="$(realpath "${FIN_AGENT_APP_DIR}")"
if [[ "${source_root}" != "${app_root}" && ! -d "${FIN_AGENT_APP_DIR}/.git" ]]; then
    if [[ -d "${PROJECT_ROOT}/.git" ]]; then
        rsync -a \
            --exclude '.env' \
            --exclude '.venv' \
            --exclude 'venv' \
            --exclude 'certs' \
            --exclude 'data' \
            --exclude '.secrets' \
            --exclude 'admin_2fa_qr.html' \
            --exclude '__pycache__' \
            "${PROJECT_ROOT}/" "${FIN_AGENT_APP_DIR}/"
        if [[ -f "${PROJECT_ROOT}/.env" ]]; then
            mv "${PROJECT_ROOT}/.env" "${FIN_AGENT_APP_DIR}/.env"
            chmod 600 "${FIN_AGENT_APP_DIR}/.env"
        fi
        if [[ -f "${PROJECT_ROOT}/data/gcp-service-account.json" ]]; then
            mkdir -p "${FIN_AGENT_APP_DIR}/data"
            mv "${PROJECT_ROOT}/data/gcp-service-account.json" "${FIN_AGENT_APP_DIR}/data/gcp-service-account.json"
            chmod 600 "${FIN_AGENT_APP_DIR}/data/gcp-service-account.json"
        fi
    fi
fi

if [[ -L "${FIN_AGENT_APP_LINK}" || ! -e "${FIN_AGENT_APP_LINK}" ]]; then
    ln -sfn "${FIN_AGENT_APP_DIR}" "${FIN_AGENT_APP_LINK}"
else
    echo "[-] ${FIN_AGENT_APP_LINK} уже существует и не является ссылкой."
    echo "    Удалите этот каталог после проверки и выполните: ln -sfn ${FIN_AGENT_APP_DIR} ${FIN_AGENT_APP_LINK}"
fi
if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    chown -R "${SUDO_USER}:${FIN_AGENT_GROUP}" "${FIN_AGENT_APP_DIR}"
    if [[ -f "${FIN_AGENT_APP_DIR}/.env" ]]; then
        chmod 600 "${FIN_AGENT_APP_DIR}/.env"
        chown "${SUDO_USER}:${FIN_AGENT_GROUP}" "${FIN_AGENT_APP_DIR}/.env"
    fi
fi
chmod 750 "${FIN_AGENT_MOUNT}"
chown "root:${FIN_AGENT_GROUP}" "${FIN_AGENT_MOUNT}"
chmod 700 "${FIN_AGENT_DOCKER_DIR}"
chown root:root "${FIN_AGENT_DOCKER_DIR}"

systemctl start fin-agent-storage.service
systemctl start docker

echo "[+] Хост подготовлен."
echo "    Клон: ${FIN_AGENT_APP_LINK}"
if [[ ! -d "${FIN_AGENT_APP_DIR}/.git" ]]; then
    echo "    Склонируйте репозиторий в ${FIN_AGENT_APP_DIR} и повторите ссылку ${FIN_AGENT_APP_LINK}."
fi
echo "    Положите ключ GCP в ${FIN_AGENT_APP_LINK}/data/gcp-service-account.json и выставьте режим 0600."
echo "    Переподключитесь по SSH, чтобы применились группы docker и ${FIN_AGENT_GROUP}."
echo "    Затем из клона: bash scripts/deploy_vds_prod.sh"
echo "    После перезагрузки: sudo bash scripts/unlock_volumes.sh"
