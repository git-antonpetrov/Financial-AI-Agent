#!/usr/bin/env bash
# Создаёт файловый том LUKS2 и переносит data-root Docker на него.
# Парольную фразу читает с терминала и никуда не записывает.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/paths.sh
source "${SCRIPT_DIR}/lib/paths.sh"

VOLUME_NAME="${FIN_AGENT_VOLUME_NAME}"
MOUNT_POINT="${FIN_AGENT_MOUNT}"
MAPPER_NAME="${FIN_AGENT_MAPPER}"
STORAGE_DIR="${FIN_AGENT_STORAGE}"
CONTAINER_FILE="${FIN_AGENT_CONTAINER}"
DOCKER_DIR="${FIN_AGENT_DOCKER_DIR}"
SIZE_GB="${FIN_AGENT_VOLUME_GB:-20}"

docker_daemon_is_running() {
    if command -v systemctl >/dev/null 2>&1; then
        if systemctl is-active --quiet docker; then
            return 0
        fi
    fi
    if command -v pgrep >/dev/null 2>&1; then
        if pgrep -x dockerd >/dev/null 2>&1; then
            return 0
        fi
    fi
    return 1
}

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        echo "[-] Скрипт запускается только от root." >&2
        exit 1
    fi
}

require_tty() {
    if [[ ! -t 0 ]]; then
        echo "[-] Нужна интерактивная сессия оператора. GitHub Action этот шаг не выполняет." >&2
        exit 1
    fi
}

read_passphrase() {
    local first=""
    local second=""
    read -r -s -p "Введите парольную фразу LUKS (не короче 16 символов): " first
    printf '\n'
    read -r -s -p "Повторите парольную фразу: " second
    printf '\n'
    if [[ "${#first}" -lt 16 ]]; then
        echo "[-] Парольная фраза короче 16 символов." >&2
        exit 1
    fi
    if [[ "${first}" != "${second}" ]]; then
        echo "[-] Парольные фразы не совпали." >&2
        exit 1
    fi
    PASSPHRASE="${first}"
    unset first second
}

run_cryptsetup() {
    printf '%s' "${PASSPHRASE}" | cryptsetup "$@" --key-file=-
}

append_once() {
    local file="$1"
    local line="$2"
    mkdir -p "$(dirname "${file}")"
    touch "${file}"
    if ! grep -Fxq "${line}" "${file}"; then
        printf '%s\n' "${line}" >> "${file}"
    fi
}

require_root
require_tty

for cmd in cryptsetup mkfs.ext4 blkid rsync mountpoint; do
    if ! command -v "${cmd}" >/dev/null 2>&1; then
        echo "[-] Не найдена утилита ${cmd}." >&2
        exit 1
    fi
done

if [[ "${MOUNT_POINT}" == "/var/lib/docker" || "${MOUNT_POINT}" == "/var/lib/docker/volumes" ]]; then
    echo "[-] Ошибка: нельзя монтировать том поверх живого каталога Docker ${MOUNT_POINT}." >&2
    exit 1
fi

if docker_daemon_is_running; then
    echo "[-] Ошибка: нельзя монтировать том поверх живого каталога Docker и переносить data-root, пока служба запущена." >&2
    echo "    Остановите Docker и запустите скрипт снова." >&2
    exit 1
fi

mkdir -p "${STORAGE_DIR}"
chmod 700 "${STORAGE_DIR}"

if [[ ! -f "${CONTAINER_FILE}" ]]; then
    echo "[+] Выделяет контейнер ${SIZE_GB} ГБ: ${CONTAINER_FILE}"
    if ! fallocate -l "${SIZE_GB}G" "${CONTAINER_FILE}"; then
        dd if=/dev/zero of="${CONTAINER_FILE}" bs=1M count="$((SIZE_GB * 1024))" status=progress
    fi
    chmod 600 "${CONTAINER_FILE}"
    read_passphrase
    echo "[+] Форматирует LUKS2 aes-xts-plain64, ключ 512 бит."
    run_cryptsetup luksFormat \
        --type luks2 \
        --cipher aes-xts-plain64 \
        --key-size 512 \
        --hash sha512 \
        --pbkdf argon2id \
        --batch-mode \
        "${CONTAINER_FILE}"
else
    echo "[*] Контейнер уже существует: ${CONTAINER_FILE}"
    read_passphrase
fi

if [[ ! -b "/dev/mapper/${MAPPER_NAME}" ]]; then
    echo "[+] Открывает том ${MAPPER_NAME}."
    run_cryptsetup open --type luks2 "${CONTAINER_FILE}" "${MAPPER_NAME}"
fi
unset PASSPHRASE

if ! blkid "/dev/mapper/${MAPPER_NAME}" | grep -q 'ext4'; then
    echo "[+] Создаёт файловую систему ext4."
    mkfs.ext4 -L "${VOLUME_NAME}" "/dev/mapper/${MAPPER_NAME}"
fi

mkdir -p "${MOUNT_POINT}"
if ! mountpoint -q "${MOUNT_POINT}"; then
    mount -o noatime,nodev,nosuid "/dev/mapper/${MAPPER_NAME}" "${MOUNT_POINT}"
fi
chmod 750 "${MOUNT_POINT}"
if getent group "${FIN_AGENT_GROUP}" >/dev/null 2>&1; then
    chown "root:${FIN_AGENT_GROUP}" "${MOUNT_POINT}"
fi

mkdir -p "${DOCKER_DIR}" "${FIN_AGENT_APP_DIR}"
chmod 700 "${DOCKER_DIR}"

append_once /etc/crypttab "${MAPPER_NAME} ${CONTAINER_FILE} none luks,noauto"
append_once /etc/fstab "/dev/mapper/${MAPPER_NAME} ${MOUNT_POINT} ext4 noatime,nodev,nosuid,noauto 0 2"

if [[ -d /var/lib/docker ]]; then
    current_root="$(realpath /var/lib/docker)"
    next_root="$(realpath "${DOCKER_DIR}")"
    case "${next_root}" in
        "${current_root}"|"${current_root}"/*)
            echo "[-] data-root назначения находится внутри текущего каталога Docker." >&2
            exit 1
            ;;
    esac
    if [[ "${current_root}" != "${next_root}" ]]; then
        echo "[+] Переносит data-root Docker на ${DOCKER_DIR}."
        rsync -aHAX /var/lib/docker/ "${DOCKER_DIR}/"
        mkdir -p /etc/docker
        if [[ ! -f /etc/docker/daemon.json ]]; then
            printf '%s\n' "{\"data-root\":\"${DOCKER_DIR}\"}" > /etc/docker/daemon.json
            chmod 644 /etc/docker/daemon.json
        elif ! grep -Fq "\"${DOCKER_DIR}\"" /etc/docker/daemon.json; then
            echo "[-] /etc/docker/daemon.json не указывает data-root на ${DOCKER_DIR}." >&2
            exit 1
        fi
        rm -rf /var/lib/docker
        if command -v fstrim >/dev/null 2>&1; then
            fstrim -v / || true
        fi
    fi
else
    mkdir -p /etc/docker
    if [[ ! -f /etc/docker/daemon.json ]]; then
        printf '%s\n' "{\"data-root\":\"${DOCKER_DIR}\"}" > /etc/docker/daemon.json
        chmod 644 /etc/docker/daemon.json
    fi
fi

mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/fin-agent-storage.service <<EOF
[Unit]
Description=Проверяет, что том Financial AI Agent смонтирован
DefaultDependencies=no

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$(command -v mountpoint) -q ${MOUNT_POINT}

[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/docker.service.d/fin-agent-luks.conf <<EOF
[Unit]
Requires=fin-agent-storage.service
After=fin-agent-storage.service
EOF
systemctl daemon-reload
systemctl disable docker >/dev/null 2>&1 || true

echo "[+] Том LUKS2 смонтирован в ${MOUNT_POINT}."
echo "    Docker не стартует при загрузке. После перезагрузки: sudo bash scripts/unlock_volumes.sh"
