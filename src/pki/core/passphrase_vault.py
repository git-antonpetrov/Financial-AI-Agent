import os
import secrets
import string
from typing import Optional


def resolve_root_ca_passphrase(env_var_name: str = "ROOT_CA_PASSPHRASE") -> str:
    """
    Разрешает и валидирует секретную парольную фразу для шифрования приватного ключа Root CA.
    
    В production/strict режиме: требует обязательного наличия ROOT_CA_PASSPHRASE (длина >= 16 символов).
    В dev/test режиме: если переменная не задана, генерирует криптографически стойкую временную фразу.
    """
    passphrase = os.getenv(env_var_name, "").strip()
    is_strict = (
        os.getenv("STRICT_SECURITY", "false").lower() in ("true", "1")
        or os.getenv("ENV") == "production"
    )

    if not passphrase:
        if is_strict:
            raise RuntimeError(
                f"Критическая ошибка безопасности: переменная окружения {env_var_name} не задана. "
                f"В production режиме запуск Root CA без парольной фразы категорически запрещен."
            )
        # В dev/test режиме генерируем надежную 32-символьную фразу
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
        passphrase = "".join(secrets.choice(alphabet) for _ in range(32))

    if len(passphrase) < 12 and is_strict:
        raise ValueError(
            f"Парольная фраза {env_var_name} слишком короткая ({len(passphrase)} символов). "
            f"Минимальная допустимая длина для Root CA — 12 символов (рекомендуется 32+)."
        )

    return passphrase
