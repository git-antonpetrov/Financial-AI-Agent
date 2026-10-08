"""
Модуль конвертного шифрования (Envelope Encryption) для векторной базы данных ChromaDB.
Обеспечивает шифрование открытого текста чанков и чувствительных метаданных с использованием
аутентифицированного алгоритма AES-256-GCM (NIST SP 800-38D).

Защищает данные в хранилище ChromaDB от несанкционированного доступа при сохранении
работоспособности семантического поиска по сгенерированным эмбеддингам.
"""

import os
import base64
import hashlib
from typing import Any
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from core.utils.console_logger import log_warning, log_info

MAGIC_ENVELOPE_V1 = b"ENC1"
_EPHEMERAL_KEY: bytes | None = None

# Ключи метаданных, сохраняемые в открытом виде для фильтрации ChromaDB (where={"short_name": ...})
DEFAULT_PRESERVED_KEYS = {
    "short_name",
    "system_name",
    "chunk_index",
    "is_encrypted",
    "_encrypted",
}


def is_envelope_encryption_enabled() -> bool:
    """Проверяет, включено ли конвертное шифрование для ChromaDB."""
    val = os.getenv("CHROMA_ENVELOPE_ENCRYPTION_ENABLED", "true").strip().lower()
    return val in ("true", "1", "yes", "on")


def resolve_encryption_key(key: bytes | str | None = None) -> bytes:
    """
    Разрешает и валидирует 256-битный (32-байтный) ключ шифрования AES-GCM.
    Принимает:
      - bytes (ровно 32 байта)
      - hex-строку (64 символа)
      - base64-строку (32 декодированных байта)
      - произвольную строку (деривация через SHA-256)
      - None (берется из DATA_ENCRYPTION_KEY / CHROMA_ENVELOPE_KEY или генерируется эфемерный ключ)
    """
    global _EPHEMERAL_KEY
    if key is None:
        raw_key = os.getenv("CHROMA_ENVELOPE_KEY") or os.getenv("DATA_ENCRYPTION_KEY", "")
        raw_key = raw_key.strip()
        if not raw_key:
            is_strict = os.getenv("STRICT_SECURITY", "false").lower() in ("true", "1") or os.getenv("ENV") == "production"
            if is_strict:
                raise RuntimeError(
                    "Ключ шифрования ChromaDB (DATA_ENCRYPTION_KEY / CHROMA_ENVELOPE_KEY) не настроен. "
                    "В production/strict режиме генерация эфемерных ключей запрещена."
                )
            if _EPHEMERAL_KEY is None:
                _EPHEMERAL_KEY = os.urandom(32)
                log_warning(
                    "ChromaDB Envelope",
                    "Ключ шифрования ChromaDB не настроен в окружении (DATA_ENCRYPTION_KEY). "
                    "Сгенерирован временный 256-битный ключ в памяти (dev/test режим)."
                )
            return _EPHEMERAL_KEY
        key = raw_key

    if isinstance(key, (bytes, bytearray)):
        if len(key) == 32:
            return bytes(key)
        raise ValueError(f"Ключ шифрования (bytes) должен быть ровно 32 байта, получено: {len(key)}")

    if isinstance(key, str):
        key = key.strip()
        if not key:
            raise ValueError("Ключ шифрования не может быть пустой строкой")
        # 1. 64-символьный hex
        if len(key) == 64:
            try:
                candidate = bytes.fromhex(key)
                if len(candidate) == 32:
                    return candidate
            except ValueError:
                pass
        # 2. Base64
        try:
            candidate = base64.b64decode(key)
            if len(candidate) == 32:
                return candidate
        except Exception:
            pass
        # 3. 32 ASCII-символа
        encoded = key.encode("utf-8")
        if len(encoded) == 32:
            return encoded
        # 4. Деривация через SHA-256
        return hashlib.sha256(encoded).digest()

    raise TypeError(f"Неподдерживаемый тип ключа шифрования: {type(key).__name__}")


def encrypt_chroma_chunk(
    chunk: str,
    key: bytes | str | None = None,
    aad: bytes | str | None = None
) -> str:
    """
    Шифрует текстовый чанк с использованием AES-256-GCM.
    Возвращает Base64url строку с заголовком ENC1.
    """
    if not isinstance(chunk, str):
        chunk = str(chunk)

    resolved_key = resolve_encryption_key(key)
    nonce = os.urandom(12)
    aesgcm = AESGCM(resolved_key)

    aad_bytes = None
    if aad is not None:
        aad_bytes = aad.encode("utf-8") if isinstance(aad, str) else bytes(aad)

    ciphertext_and_tag = aesgcm.encrypt(nonce, chunk.encode("utf-8"), aad_bytes)
    raw_payload = MAGIC_ENVELOPE_V1 + nonce + ciphertext_and_tag
    return base64.urlsafe_b64encode(raw_payload).decode("ascii")


_BASE64URL_ALPHABET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")
_MIN_ENVELOPE_BYTES_LEN = len(MAGIC_ENVELOPE_V1) + 12 + 16  # Magic (4) + Nonce (12) + Tag (16) = 32 байта
_MIN_ENVELOPE_B64_LEN = 43  # 32 байта в Base64url кодируются в 43 символа без padding (44 с padding)
_ENVELOPE_B64_PREFIX = "RU5DM"  # b"ENC1" в Base64url: b"ENC" -> "RU5D", b"1" + 2 бита nonce всегда дают "M"


def is_encrypted_chunk(val: Any) -> bool:
    """
    Строго проверяет, является ли значение строкой зашифрованного пакета ENC1 (Envelope Encryption).
    
    В отличие от примитивной эвристики startswith("RU5D"), данная функция исключает
    ложные срабатывания (false positives) для обычных строк, случайно начинающихся с 'RU5D'
    (например, 'RU5D', 'RU5D (Текст документа)', 'RU5D-2026' и т.д.).
    
    Критерии:
    1. Значение должно быть непустой строкой длиной не менее 43 символов.
    2. Должно начинаться с детерминированного префикса 'RU5DM' (Base64url от b"ENC1").
    3. Должно состоять строго из алфавита Base64url (без пробелов, переводов строк и спецсимволов).
    4. Должно успешно декодироваться в Base64url и содержать заголовок b"ENC1" длиной >= 32 байт.
    """
    if not isinstance(val, str) or len(val) < _MIN_ENVELOPE_B64_LEN:
        return False

    if not val.startswith(_ENVELOPE_B64_PREFIX):
        return False

    if any(c not in _BASE64URL_ALPHABET for c in val):
        return False

    try:
        raw_payload = base64.urlsafe_b64decode(val.encode("ascii"))
    except Exception:
        return False

    return (
        len(raw_payload) >= _MIN_ENVELOPE_BYTES_LEN
        and raw_payload.startswith(MAGIC_ENVELOPE_V1)
    )


def decrypt_chroma_chunk(
    val: str,
    key: bytes | str | None = None,
    aad: bytes | str | None = None,
    is_encrypted: bool | None = None
) -> str:
    """
    Расшифровывает чанк, сохраненный в ChromaDB.
    Если строка не является зашифрованным пакетом ENC1 (унаследованные данные),
    возвращает её без изменений (обратная совместимость).
    
    Параметр is_encrypted (опционально) позволяет использовать значение флага
    из метаданных документа (is_encrypted в ChromaDB) для точного решения.
    """
    if not isinstance(val, str) or not val:
        return val

    # Если в метаданных явно указано, что документ не зашифрован (legacy)
    if is_encrypted is False:
        return val

    # Строгая валидация формата зашифрованного конверта ENC1 вместо наивной эвристики "RU5D"
    if not is_encrypted_chunk(val):
        if is_encrypted is True:
            raise ValueError("Ошибка расшифровки чанка ChromaDB: нарушена структура зашифрованного пакета ENC1.")
        return val

    try:
        raw_payload = base64.urlsafe_b64decode(val.encode("ascii"))
    except Exception as e:
        if is_encrypted is True:
            raise ValueError("Ошибка расшифровки чанка ChromaDB: невалидная Base64url строка.") from e
        return val

    header_len = len(MAGIC_ENVELOPE_V1)
    nonce = raw_payload[header_len : header_len + 12]
    ciphertext_and_tag = raw_payload[header_len + 12 :]

    resolved_key = resolve_encryption_key(key)
    aesgcm = AESGCM(resolved_key)

    aad_bytes = None
    if aad is not None:
        aad_bytes = aad.encode("utf-8") if isinstance(aad, str) else bytes(aad)

    try:
        decrypted_bytes = aesgcm.decrypt(nonce, ciphertext_and_tag, aad_bytes)
        return decrypted_bytes.decode("utf-8")
    except Exception as e:
        raise ValueError("Ошибка расшифровки чанка ChromaDB: нарушена целостность, неверный ключ или поврежден тег аутентификации.") from e


def encrypt_chroma_metadata(
    metadata: dict[str, Any],
    key: bytes | str | None = None,
    preserve_keys: set[str] | None = None
) -> dict[str, Any]:
    """
    Шифрует чувствительные строковые поля в словаре метаданных документа.
    Сохраняет в открытом виде ключи, необходимые для фильтрации (preserve_keys).
    Добавляет флаг 'is_encrypted': True.
    """
    if not isinstance(metadata, dict):
        return {}

    preserved = preserve_keys or DEFAULT_PRESERVED_KEYS
    encrypted_meta: dict[str, Any] = {}

    for k, v in metadata.items():
        key_str = str(k)
        if key_str in preserved:
            encrypted_meta[key_str] = v
        elif isinstance(v, str):
            # Шифруем строковые значения метаданных (например, title, description)
            encrypted_meta[key_str] = encrypt_chroma_chunk(v, key=key)
        else:
            # Числовые и логические значения оставляем без изменений
            encrypted_meta[key_str] = v

    encrypted_meta["is_encrypted"] = True
    return encrypted_meta


def decrypt_chroma_metadata(
    metadata: dict[str, Any],
    key: bytes | str | None = None,
    preserve_keys: set[str] | None = None
) -> dict[str, Any]:
    """
    Расшифровывает все зашифрованные поля в словаре метаданных ChromaDB.
    """
    if not isinstance(metadata, dict):
        return metadata

    is_enc = metadata.get("is_encrypted")
    if is_enc is None:
        is_enc = metadata.get("_encrypted")

    if is_enc is False:
        return metadata

    preserved = preserve_keys or DEFAULT_PRESERVED_KEYS
    decrypted_meta: dict[str, Any] = {}
    for k, v in metadata.items():
        if k in preserved:
            decrypted_meta[k] = v
        elif isinstance(v, str):
            decrypted_meta[k] = decrypt_chroma_chunk(v, key=key)
        else:
            decrypted_meta[k] = v

    return decrypted_meta


def encrypt_batch(
    chunks: list[str],
    metadatas: list[dict[str, Any]],
    key: bytes | str | None = None,
    preserve_keys: set[str] | None = None
) -> tuple[list[str], list[dict[str, Any]]]:
    """
    Шифрует батч чанков и список соответствующих метаданных для сохранения в ChromaDB.
    """
    encrypted_chunks = [encrypt_chroma_chunk(c, key=key) for c in chunks]
    encrypted_metadatas = [encrypt_chroma_metadata(m, key=key, preserve_keys=preserve_keys) for m in metadatas]
    return encrypted_chunks, encrypted_metadatas


def decrypt_chroma_results(
    results: dict[str, Any],
    key: bytes | str | None = None
) -> dict[str, Any]:
    """
    Прозрачно расшифровывает документы и метаданные в ответе от ChromaDB (collection.query или collection.get).
    Поддерживает как вложенные списки (query), так и плоские списки (get).
    """
    if not isinstance(results, dict):
        return results

    decrypted = dict(results)

    # 1. Расшифровка метаданных
    metas = decrypted.get("metadatas")
    decrypted_metas = metas
    if metas and isinstance(metas, list):
        if metas and isinstance(metas[0], list):
            decrypted_metas = [
                [decrypt_chroma_metadata(meta, key=key) if isinstance(meta, dict) else meta for meta in meta_list]
                for meta_list in metas
            ]
        else:
            decrypted_metas = [
                decrypt_chroma_metadata(meta, key=key) if isinstance(meta, dict) else meta for meta in metas
            ]
        decrypted["metadatas"] = decrypted_metas

    # 2. Расшифровка документов (чанков) с учетом флага шифрования из метаданных
    docs = decrypted.get("documents")
    if docs and isinstance(docs, list):
        if docs and isinstance(docs[0], list):
            new_docs = []
            for i, doc_list in enumerate(docs):
                meta_list = metas[i] if (metas and i < len(metas) and isinstance(metas[i], list)) else []
                new_sublist = []
                for j, doc in enumerate(doc_list):
                    meta = meta_list[j] if (j < len(meta_list) and isinstance(meta_list[j], dict)) else None
                    is_enc = meta.get("is_encrypted") if meta else None
                    new_sublist.append(decrypt_chroma_chunk(doc, key=key, is_encrypted=is_enc))
                new_docs.append(new_sublist)
            decrypted["documents"] = new_docs
        else:
            new_docs = []
            for i, doc in enumerate(docs):
                meta = metas[i] if (metas and i < len(metas) and isinstance(metas[i], dict)) else None
                is_enc = meta.get("is_encrypted") if meta else None
                new_docs.append(decrypt_chroma_chunk(doc, key=key, is_encrypted=is_enc))
            decrypted["documents"] = new_docs

    return decrypted
