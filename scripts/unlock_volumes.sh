#!/usr/bin/env bash
# Открывает существующий том LUKS, монтирует его и запускает Docker.
# Парольную фразу читает с терминала и не сохраняет.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
if [[ ! -f "${FIN_AGENT_CONTAINER}" ]]; then
    echo "[-] Контейнер LUKS не найден: ${FIN_AGENT_CONTAINER}" >&2
    echo "    Подготовка хоста: sudo bash scripts/host_bootstrap.sh" >&2
    exit 1
fi

read -r -s -p "Парольная фраза LUKS: " PASSPHRASE
printf '\n'
if [[ -z "${PASSPHRASE}" ]]; then
    echo "[-] Пустая парольная фраза отклонена." >&2
    exit 1
fi

if [[ ! -b "/dev/mapper/${FIN_AGENT_MAPPER}" ]]; then
    printf '%s' "${PASSPHRASE}" | cryptsetup open --type luks2 --key-file=- \
        "${FIN_AGENT_CONTAINER}" "${FIN_AGENT_MAPPER}"
fi
unset PASSPHRASE

mkdir -p "${FIN_AGENT_MOUNT}"
if ! mountpoint -q "${FIN_AGENT_MOUNT}"; then
    mount -o noatime,nodev,nosuid "/dev/mapper/${FIN_AGENT_MAPPER}" "${FIN_AGENT_MOUNT}"
fi

systemctl start fin-agent-storage.service
systemctl start docker
echo "[+] Том открыт, Docker запущен. Дальше: bash scripts/deploy_vds_prod.sh"
