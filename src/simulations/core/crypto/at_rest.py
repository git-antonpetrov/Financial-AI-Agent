"""
Модуль шифрования данных при хранении (Data at Rest Encryption) для симуляций.
Реализует прозрачное конвертное шифрование AES-256-GCM для защиты финансовых реквизитов,
номеров счетов, карт и балансов в СУБД (SEC-03).
"""

import base64
import hashlib
import hmac
import os
from decimal import Decimal
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.types import Text, TypeDecorator

MAGIC_ENCRYPTION_V1 = b"ENC1"


def resolve_encryption_key(key: bytes | str | None = None) -> bytes:
    """
    Разрешает и нормализует 256-битный (32 байта) ключ шифрования.
    Приоритет:
    1. Явно переданный ключ (bytes или str).
    2. Переменная окружения DATA_ENCRYPTION_KEY или ENCRYPTION_KEY.
    3. Системный дефолтный ключ для тестирования.
    """
    raw_key = key or os.getenv("DATA_ENCRYPTION_KEY") or os.getenv("ENCRYPTION_KEY")
    if not raw_key:
        raise RuntimeError(
            "DATA_ENCRYPTION_KEY должен быть задан в окружении для шифрования Data at Rest."
        )

    if isinstance(raw_key, (bytes, bytearray)):
        if len(raw_key) != 32:
            raise ValueError(f"Бинарный ключ шифрования должен быть ровно 32 байта, получено: {len(raw_key)}")
        return bytes(raw_key)

    if isinstance(raw_key, str):
        raw_key = raw_key.strip()
        if not raw_key:
            raise ValueError("Ключ шифрования не может быть пустой строкой")

        # 1. Попытка парсинга как Hex (64 символа)
        if len(raw_key) == 64:
            try:
                return bytes.fromhex(raw_key)
            except ValueError:
                pass

        # 2. Попытка Base64 (32 байта)
        try:
            padded = raw_key + "=" * (-len(raw_key) % 4)
            decoded = base64.b64decode(padded)
            if len(decoded) == 32:
                return decoded
        except Exception:
            pass

        # 3. Точно 32 байта ASCII
        encoded = raw_key.encode("utf-8")
        if len(encoded) == 32:
            return encoded

        # 4. Деривация через SHA-256
        return hashlib.sha256(encoded).digest()

    raise TypeError(f"Неподдерживаемый тип ключа шифрования: {type(raw_key).__name__}")


def encrypt_data_at_rest(
    data: bytes | str,
    key: bytes | str | None = None,
    associated_data: bytes | None = None,
    deterministic: bool = False,
) -> bytes:
    """
    Шифрует данные с использованием AES-256-GCM.
    Envelope: [4 байта MAGIC 'ENC1'] + [12 байт Nonce/IV] + [Шифротекст + 16 байт Auth Tag]
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    elif not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"Ожидались bytes или str, получено: {type(data).__name__}")

    resolved_key = resolve_encryption_key(key)

    if deterministic:
        # Детерминированный IV для обеспечения поиска WHERE по зашифрованным полям (searchable encryption)
        nonce = hmac.new(resolved_key, b"det_iv:" + bytes(data), hashlib.sha256).digest()[:12]
    else:
        nonce = os.urandom(12)

    aesgcm = AESGCM(resolved_key)
    ciphertext_and_tag = aesgcm.encrypt(nonce, bytes(data), associated_data)
    return MAGIC_ENCRYPTION_V1 + nonce + ciphertext_and_tag


def decrypt_data_at_rest(
    encrypted_payload: bytes,
    key: bytes | str | None = None,
    associated_data: bytes | None = None,
) -> bytes:
    """
    Расшифровывает данные с проверкой аутентичности и целостности (AEAD AES-256-GCM).
    """
    if not isinstance(encrypted_payload, (bytes, bytearray)):
        raise TypeError(f"Ожидались bytes, получено: {type(encrypted_payload).__name__}")

    min_len = len(MAGIC_ENCRYPTION_V1) + 12 + 16
    if len(encrypted_payload) < min_len:
        raise ValueError("Зашифрованные данные повреждены: длина пакета меньше минимально допустимой.")

    if not encrypted_payload.startswith(MAGIC_ENCRYPTION_V1):
        raise ValueError("Неподдерживаемый формат шифрования или отсутствующий заголовок ENC1.")

    header_len = len(MAGIC_ENCRYPTION_V1)
    nonce = encrypted_payload[header_len : header_len + 12]
    ciphertext_and_tag = encrypted_payload[header_len + 12 :]

    resolved_key = resolve_encryption_key(key)
    aesgcm = AESGCM(resolved_key)

    try:
        return aesgcm.decrypt(nonce, ciphertext_and_tag, associated_data)
    except Exception as e:
        raise ValueError("Ошибка расшифровки данных: неверный ключ или поврежденный шифротекст") from e


def encrypt_text_at_rest(
    text: str,
    key: bytes | str | None = None,
    associated_data: bytes | None = None,
    deterministic: bool = False,
) -> str:
    """Шифрует строковые данные и возвращает безопасную Base64url-строку."""
    raw_cipher = encrypt_data_at_rest(
        text.encode("utf-8"),
        key=key,
        associated_data=associated_data,
        deterministic=deterministic,
    )
    return base64.urlsafe_b64encode(raw_cipher).decode("ascii")


def decrypt_text_at_rest(
    encrypted_b64: str,
    key: bytes | str | None = None,
    associated_data: bytes | None = None,
) -> str:
    """Расшифровывает Base64url-строку и возвращает исходный UTF-8 текст."""
    if not isinstance(encrypted_b64, str):
        raise TypeError(f"Ожидалась строка Base64, получено: {type(encrypted_b64).__name__}")
    try:
        raw_cipher = base64.urlsafe_b64decode(encrypted_b64.encode("ascii"))
    except Exception as e:
        raise ValueError("Некорректная Base64 строка зашифрованных данных.") from e
    decrypted_bytes = decrypt_data_at_rest(raw_cipher, key=key, associated_data=associated_data)
    return decrypted_bytes.decode("utf-8")


class EncryptedText(TypeDecorator):
    """
    SQLAlchemy тип для прозрачного шифрования строковых данных (номера счетов, карт, кошельков)
    в PostgreSQL при хранении (Data at Rest) с использованием AES-256-GCM.
    """

    impl = Text
    cache_ok = True

    def __init__(self, deterministic: bool = True, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.deterministic = deterministic

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return encrypt_text_at_rest(str(value), deterministic=self.deterministic)

    def process_result_value(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        val_str = str(value)
        if val_str.startswith("RU5DMS"):
            return decrypt_text_at_rest(val_str)
        return val_str


class EncryptedDecimal(TypeDecorator):
    """
    SQLAlchemy тип для прозрачного шифрования балансов и финансовых сумм
    в PostgreSQL при хранении (Data at Rest) с использованием AES-256-GCM.
    При чтении восстанавливает точный объект Decimal.
    """

    impl = Text
    cache_ok = True

    def __init__(self, deterministic: bool = False, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.deterministic = deterministic

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        dec_val = Decimal(str(value)).quantize(Decimal("0.01"))
        return encrypt_text_at_rest(str(dec_val), deterministic=self.deterministic)

    def process_result_value(self, value: Any, dialect: Any) -> Decimal | None:
        if value is None:
            return None
        val_str = str(value)
        if val_str.startswith("RU5DMS"):
            decrypted = decrypt_text_at_rest(val_str)
            return Decimal(decrypted)
        try:
            return Decimal(val_str)
        except Exception:
            return None
