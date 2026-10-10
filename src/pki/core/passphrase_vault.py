import os
import sys
from typing import Optional


def resolve_root_ca_passphrase(env_var_name: str = "ROOT_CA_PASSPHRASE") -> str:
    """
    Разрешает и валидирует секретную парольную фразу для шифрования приватного ключа Root CA.
    
    В соответствии со строгой Zero-Trust моделью:
    - Требует обязательного наличия ROOT_CA_PASSPHRASE в переменных окружения.
    - Минимальная допустимая длина — 16 символов.
    - Эфемерная генерация паролей в оперативной памяти запрещена.
    """
    passphrase = os.getenv(env_var_name, "").strip()

    if not passphrase:
        raise RuntimeError(
            f"Критическая ошибка безопасности: переменная окружения {env_var_name} не задана. "
            f"Запуск Root CA без парольной фразы категорически запрещен."
        )

    if len(passphrase) < 16:
        raise ValueError(
            f"Парольная фраза {env_var_name} слишком короткая ({len(passphrase)} символов). "
            f"Минимальная допустимая длина для Root CA — 16 символов (рекомендуется 32+)."
        )

    return passphrase
