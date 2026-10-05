"""
Пользовательские типы данных SQLAlchemy для шифрования данных при хранении (Data at Rest).
Обеспечивают прозрачное шифрование конфиденциальных полей базы данных с помощью AES-256-GCM.
"""

from typing import Any
from sqlalchemy.types import TypeDecorator, Text


class EncryptedText(TypeDecorator):
    """
    SQLAlchemy тип для прозрачного шифрования текстовых данных при записи в БД (Data at Rest)
    и расшифровки при чтении с использованием алгоритма AES-256-GCM.
    
    Обеспечивает обратную совместимость: если значение в БД сохранено в открытом виде (до внедрения шифрования),
    оно возвращается без изменений (safe fallback / seamless migration).
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
        try:
            from security import decrypt_text_at_rest
        except ImportError:
            try:
                from ..security import decrypt_text_at_rest
            except ImportError:
                from src.admin_server.fastapi.security import decrypt_text_at_rest
        try:
            return decrypt_text_at_rest(str(value))
        except Exception:
            # Обратная совместимость с ранее сохраненным открытым текстом
            return str(value)
