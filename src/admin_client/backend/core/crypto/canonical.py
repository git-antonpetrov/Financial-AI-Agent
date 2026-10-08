"""
Каноническая сериализация данных для цифровой подписи (Zero-Trust Pipeline).
Обеспечивает строгое совпадение дайджестов между Admin Client и Admin Server.
"""

import hashlib
import json
from typing import Any, Union


def compute_rag_canonical_digest(timestamp: Union[int, str], nonce: str, body_bytes: bytes) -> bytes:
    """
    Вычисляет канонические байты для цифровой подписи запроса загрузки документа:
    Canonical_String = Timestamp + "\\n" + Nonce + "\\n" + SHA256(Body)
    """
    body_digest = hashlib.sha256(body_bytes).hexdigest()
    canonical_str = f"{timestamp}\n{nonce}\n{body_digest}"
    return canonical_str.encode("utf-8")


def compute_receipt_canonical_bytes(receipt_dict: dict[str, Any]) -> bytes:
    """
    Вычисляет канонические байты для проверки цифровой подписи квитанции сервера (AckReceipt).
    Исключает поле 'signature' (если оно присутствует в словаре) и сериализует с сортировкой ключей.
    """
    clean_data = {k: v for k, v in receipt_dict.items() if k not in ("signature", "server_key_fingerprint")}
    return json.dumps(clean_data, sort_keys=True, ensure_ascii=False).encode("utf-8")
