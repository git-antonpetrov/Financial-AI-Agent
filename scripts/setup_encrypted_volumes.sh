#!/usr/bin/env bash
# ==============================================================================
# Скрипт инициализации шифрованных томов (Data at Rest / LUKS2)
# Соответствие стандартам безопасности: PCI-DSS 3.4, GDPR Art. 32, ГОСТ Р 57580
# ==============================================================================
set -euo pipefail

VOLUME_NAME=${1:-"fin_agent_secure_data"}
MOUNT_POINT=${2:-"/var/lib/docker/volumes/financial_secure_data/_data"}
KEYFILE=${3:-"/etc/security/luks_keys/fin_agent.key"}
SIZE_GB=${4:-20}
STORAGE_DIR="/var/lib/secure_volumes"

echo "======================================================================"
echo " [Enterprise Security] Настройка шифрованного тома LUKS2: ${VOLUME_NAME}"
echo "======================================================================"

if [[ $EUID -ne 0 ]]; then
   echo "[-] Ошибка: Данный скрипт должен быть запущен с правами root (sudo)."
   exit 1
fi

# 1. Проверка утилит
for cmd in cryptsetup dd mkfs.ext4 blkid; do
    if ! command -v "$cmd" &> /dev/null; then
        echo "[-] Ошибка: Утилита $cmd не найдена. Установите: apt-get install cryptsetup e2fsprogs"
        exit 1
    fi
done

# 2. Создание защищенного каталога ключей
mkdir -p "$(dirname "$KEYFILE")"
chmod 700 "$(dirname "$KEYFILE")"

if [[ ! -f "$KEYFILE" ]]; then
    echo "[+] Генерация 512-битного случайного ключа для LUKS2..."
    dd if=/dev/urandom of="$KEYFILE" bs=64 count=1 status=none
    chmod 400 "$KEYFILE"
    echo "[+] Ключ сохранен с правами 0400: ${KEYFILE}"
else
    echo "[*] Используется существующий ключ: ${KEYFILE}"
fi

# 3. Создание блочного файла-контейнера
mkdir -p "$STORAGE_DIR"
CONTAINER_FILE="${STORAGE_DIR}/${VOLUME_NAME}.img"

if [[ ! -f "$CONTAINER_FILE" ]]; then
    echo "[+] Выделение файла-контейнера объемом ${SIZE_GB}GB..."
    fallocate -l "${SIZE_GB}G" "$CONTAINER_FILE" || dd if=/dev/zero of="$CONTAINER_FILE" bs=1M count=$((SIZE_GB * 1024)) status=progress
    chmod 600 "$CONTAINER_FILE"
    
    echo "[+] Форматирование контейнера LUKS2 (AES-XTS-plain64, 512-bit)..."
    cryptsetup luksFormat --type luks2 --cipher aes-xts-plain64 --key-size 512 --hash sha512 --pbkdf argon2id --key-file "$KEYFILE" "$CONTAINER_FILE"
else
    echo "[*] Файл-контейнер уже существует: ${CONTAINER_FILE}"
fi

# 4. Открытие зашифрованного устройства
MAPPER_NAME="dm_${VOLUME_NAME}"
if [[ ! -b "/dev/mapper/${MAPPER_NAME}" ]]; then
    echo "[+] Открытие зашифрованного тома через dm-crypt..."
    cryptsetup open --type luks2 --key-file "$KEYFILE" "$CONTAINER_FILE" "$MAPPER_NAME"
fi

# 5. Создание файловой системы ext4, если она еще не создана
if ! blkid "/dev/mapper/${MAPPER_NAME}" | grep -q "ext4"; then
    echo "[+] Создание файловой системы ext4 на зашифрованном устройстве..."
    mkfs.ext4 -L "$VOLUME_NAME" "/dev/mapper/${MAPPER_NAME}"
fi

# 6. Монтирование в точку назначения
mkdir -p "$MOUNT_POINT"
if ! mount | grep -q "/dev/mapper/${MAPPER_NAME}"; then
    echo "[+] Монтирование зашифрованного тома в ${MOUNT_POINT}..."
    mount -o noatime,nodev,nosuid "/dev/mapper/${MAPPER_NAME}" "$MOUNT_POINT"
fi

chmod 700 "$MOUNT_POINT"

echo "======================================================================"
echo "[+] УСПЕШНО: Зашифрованный том LUKS2 смонтирован и готов к работе:"
echo "    Точка монтирования: ${MOUNT_POINT}"
echo "    Блочное устройство: /dev/mapper/${MAPPER_NAME}"
echo "    Шифр: AES-XTS 512-bit (Argon2id KDF)"
echo "======================================================================"
