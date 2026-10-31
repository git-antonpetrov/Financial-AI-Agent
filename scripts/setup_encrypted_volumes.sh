#!/usr/bin/env bash
# ==============================================================================
# Скрипт инициализации шифрованных томов (Data at Rest / LUKS2)
# Соответствие стандартам безопасности: PCI-DSS 3.4, GDPR Art. 32, ГОСТ Р 57580
# ==============================================================================
set -euo pipefail

VOLUME_NAME=${1:-"fin_agent_secure_data"}
MOUNT_POINT=${2:-"/mnt/fin_agent_encrypted_volumes"}
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

# Останавливает монтирование поверх уже занятого каталога Docker, потому что
# такой mount скрывает существующие тома.
if [[ "$MOUNT_POINT" == "/var/lib/docker/volumes" || "$MOUNT_POINT" == "/var/lib/docker" ]]; then
    if [[ "${FIN_AGENT_FORCE_MOUNT:-}" != "1" ]]; then
        echo "[-] Ошибка: точка монтирования ${MOUNT_POINT} совпадает с каталогом Docker."
        echo "    Укажите отдельный каталог и перенесите data-root Docker на расшифрованный том."
        echo "    Повторный запуск поверх живых томов требует FIN_AGENT_FORCE_MOUNT=1."
        exit 1
    fi
fi

if [[ -d "$MOUNT_POINT" ]] && [[ -n "$(ls -A "$MOUNT_POINT" 2>/dev/null || true)" ]]; then
    if [[ "${FIN_AGENT_FORCE_MOUNT:-}" != "1" ]]; then
        echo "[-] Ошибка: каталог ${MOUNT_POINT} не пуст. Монтирование прервано."
        exit 1
    fi
fi

mkdir -p "$STORAGE_DIR"
mkdir -p "$(dirname "$KEYFILE")"

# Отклоняет ключ на том же блочном устройстве, что и контейнер LUKS, потому что
# снимок диска хостера тогда содержит и ключ, и шифротекст.
container_fs=$(df -P "$STORAGE_DIR" | awk 'NR==2 {print $1}')
key_fs=$(df -P "$(dirname "$KEYFILE")" | awk 'NR==2 {print $1}')
if [[ "$container_fs" == "$key_fs" && "${FIN_AGENT_ALLOW_SAME_DISK_LUKS_KEY:-}" != "1" ]]; then
    echo "[-] Ошибка: ключ ${KEYFILE} и контейнер ${STORAGE_DIR} лежат на одном устройстве ${container_fs}."
    echo "    Передайте ключ с носителя вне снимка виртуальной машины или через TPM."
    exit 1
fi

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
