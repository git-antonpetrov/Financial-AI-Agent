"""
Пользовательские типы данных SQLAlchemy для шифрования данных при хранении (Data at Rest).
Обеспечивают прозрачное шифрование конфиденциальных полей базы данных с помощью AES-256-GCM.
"""

import base64
from typing import Any
from sqlalchemy.types import TypeDecorator, Text


class EncryptedText(TypeDecorator):
    """
    SQLAlchemy тип для прозрачного шифрования текстовых данных при записи в БД (Data at Rest)
    и расшифровки при чтении с использованием алгоритма AES-256-GCM.
    
    Обеспечивает обратную совместимость: если значение в БД сохранено в открытом виде (до внедрения шифрования),
    оно возвращается без изменений (safe fallback / seamless migration).
    Для зашифрованных значений (префикс ENC1 или Base64 с сигнатурой ENC1) при ошибке расшифровки
    выбрасывается исключение ValueError во избежание утечки шифротекста или молчаливого повреждения данных.
    """
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        try:
            from security import encrypt_text_at_rest
        except ImportError:
            try:
                from ..security import encrypt_text_at_rest
            except ImportError:
                from src.admin_server.fastapi.security import encrypt_text_at_rest
        return encrypt_text_at_rest(str(value))

    def process_result_value(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        val_str = str(value)
        is_enc1 = False
        if val_str.startswith("ENC1"):
            is_enc1 = True
        else:
            try:
                padded = val_str + "=" * (-len(val_str) % 4)
                if base64.urlsafe_b64decode(padded[:8].encode("ascii")).startswith(b"ENC1"):
                    is_enc1 = True
            except Exception:
                pass

        try:
            from security import decrypt_text_at_rest
        except ImportError:
            try:
                from ..security import decrypt_text_at_rest
            except ImportError:
                from src.admin_server.fastapi.security import decrypt_text_at_rest
        try:
            return decrypt_text_at_rest(val_str)
        except Exception as e:
            if is_enc1:
                raise ValueError(f"Decryption failed for ENC1 encrypted payload: {e}") from e
            # Обратная совместимость с ранее сохраненным открытым текстом
            return val_str
