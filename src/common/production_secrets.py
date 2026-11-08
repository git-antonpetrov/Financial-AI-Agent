"""Отклоняет известные плейсхолдеры секретов в строгом режиме."""

from __future__ import annotations

import os
from collections.abc import Mapping

_SECRET_NAME_MARKERS: tuple[str, ...] = ("password", "secret", "token", "key")

_EXACT_PLACEHOLDER_SECRETS: frozenset[str] = frozenset(
    {
        "change_me",
        "secret",
        "admin123",
        "minioadmin",
        "financial-core-proxy-token-2026",
        "change_this_proxy_internal_secret",
        "change_this_strong_password_in_production",
        "change_this_bank_db_password",
        "change_this_invest_db_password",
        "change_this_digital_db_password",
        "change_this_redis_password",
        "change_this_pgadmin_password",
        "minio_access_key_placeholder",
        "minio_secret_key_placeholder",
        "chroma_token_secret_placeholder",
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        "bootstrap_token_digital_agent_secret",
        "bootstrap_token_bank_agent_secret",
        "bootstrap_token_invest_agent_secret",
        "bootstrap_token_main_agent_secret",
        "bootstrap_token_oracle_secret",
        "bootstrap_token_oracle_agent_secret",
    }
)

_MINIO_KMS_PLACEHOLDER_MATERIAL = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
_TOTP_PLACEHOLDER_PREFIX = "JBSWY3DPEHPK3PXP"


def strict_security_enabled() -> bool:
    """Сообщает, включён ли строгий режим по переменной STRICT_SECURITY.

    Возвращает:
        True для значений true, 1 и yes без учёта регистра. Пустое значение
        считается включённым, потому что боевой compose задаёт тот же умолчательный режим.
    """
    return os.getenv("STRICT_SECURITY", "true").lower() in ("true", "1", "yes")


def _name_carries_secret(name: str) -> bool:
    """Сообщает, относится ли имя переменной к паролю, секрету, токену или ключу."""
    lowered = name.lower()
    return any(marker in lowered for marker in _SECRET_NAME_MARKERS)


def is_known_placeholder_secret(name: str, value: str) -> bool:
    """Сообщает, совпадает ли значение с плейсхолдером из примера окружения.

    Аргументы:
        name: Имя переменной. Проверка TOTP и материала KMS смотрит на это имя.
        value: Значение секрета. Пробелы по краям не учитываются.

    Возвращает:
        True, если значение нельзя принимать как боевой секрет.
    """
    text = value.strip()
    if not text:
        return False
    lowered_name = name.lower()
    if text.lower() in _EXACT_PLACEHOLDER_SECRETS:
        return True
    if "totp" in lowered_name and text.upper().startswith(_TOTP_PLACEHOLDER_PREFIX):
        return True
    if "kms" in lowered_name and _MINIO_KMS_PLACEHOLDER_MATERIAL in text:
        return True
    return False


def reject_known_placeholder_secrets(extra: Mapping[str, str] | None = None) -> None:
    """Останавливает строгий запуск, если секрет совпадает с известным плейсхолдером.

    При выключенном STRICT_SECURITY ничего не проверяет: тестовый ключ из примера
    остаётся допустимым, пока тест явно не включает строгий режим.

    Аргументы:
        extra: Дополнительные пары имя-значение, которые уже загружены в объект
            настроек и могут отличаться от текущего окружения.

    Вызывает:
        RuntimeError: Строгий режим включён и найден плейсхолдер.
    """
    if not strict_security_enabled():
        return
    pairs = list(os.environ.items())
    if extra:
        pairs.extend((str(name), str(value)) for name, value in extra.items())
    for name, value in pairs:
        if not _name_carries_secret(name):
            continue
        if is_known_placeholder_secret(name, value):
            raise RuntimeError(
                f"STRICT_SECURITY: insecure default password or secret detected in {name}"
            )
