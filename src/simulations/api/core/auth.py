import os
import secrets
from typing import Optional, List
from fastapi import Header, HTTPException, status, Depends, Request
from src.simulations.core.utils.console_logger import log_warning, log_error


def extract_token(
    x_bootstrap_token: Optional[str] = Header(None, alias="X-Bootstrap-Token"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
) -> str:
    """
    Извлекает токен аутентификации из заголовков X-Bootstrap-Token или Authorization (Bearer).
    """
    if x_bootstrap_token and x_bootstrap_token.strip():
        return x_bootstrap_token.strip()

    if authorization and authorization.strip():
        parts = authorization.strip().split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1]
        elif len(parts) == 1:
            return parts[0]

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Отсутствует токен аутентификации (X-Bootstrap-Token или Authorization)"
    )


def verify_agent_token(token: str, allowed_env_vars: List[str], domain_name: str) -> str:
    """
    Проверяет валидность токена агента с защитой от атак по времени (timing attacks).
    """
    valid_tokens = []
    for var_name in allowed_env_vars:
        val = os.getenv(var_name)
        if val and val.strip():
            valid_tokens.append(val.strip())

    # Токен главного оркестратора разрешен для всех сервисов симуляции
    main_token = os.getenv("AGENT_MAIN_BOOTSTRAP_TOKEN")
    if main_token and main_token.strip():
        valid_tokens.append(main_token.strip())

    if not valid_tokens:
        log_error("Simulation Auth", f"Не сконфигурированы токены доступа для домена '{domain_name}'")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ошибка конфигурации сервера: токены для сервиса '{domain_name}' не настроены"
        )

    # Проверка совпадения токена с разрешенными
    for expected in valid_tokens:
        if secrets.compare_digest(token, expected):
            return token

    # Проверка: если токен валиден для другого домена симуляций, возвращаем 403 Forbidden
    other_env_vars = [
        "AGENT_BANK_BOOTSTRAP_TOKEN",
        "AGENT_INVEST_BOOTSTRAP_TOKEN",
        "AGENT_DIGITAL_BOOTSTRAP_TOKEN",
        "ORACLE_BOOTSTRAP_TOKEN",
        "AGENT_ORACLE_BOOTSTRAP_TOKEN",
    ]
    for var_name in other_env_vars:
        if var_name not in allowed_env_vars:
            other_val = os.getenv(var_name)
            if other_val and other_val.strip() and secrets.compare_digest(token, other_val.strip()):
                log_warning("Simulation Auth", f"Междоменный доступ к '{domain_name}' отклонен")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Доступ к сервису '{domain_name}' запрещен для данного агента"
                )

    log_warning("Simulation Auth", f"Недействительный токен при попытке обращения к '{domain_name}'")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Недействительный токен аутентификации"
    )


async def verify_bank_token(token: str = Depends(extract_token)) -> str:
    """FastAPI Dependency для проверки доступа к API банка."""
    return verify_agent_token(
        token=token,
        allowed_env_vars=["AGENT_BANK_BOOTSTRAP_TOKEN"],
        domain_name="Bank"
    )


async def verify_invest_token(token: str = Depends(extract_token)) -> str:
    """FastAPI Dependency для проверки доступа к API инвестиций."""
    return verify_agent_token(
        token=token,
        allowed_env_vars=["AGENT_INVEST_BOOTSTRAP_TOKEN"],
        domain_name="Invest"
    )


async def verify_digital_token(request: Request, token: str = Depends(extract_token)) -> str:
    """FastAPI Dependency для проверки доступа к API цифрового рубля."""
    allowed = ["AGENT_DIGITAL_BOOTSTRAP_TOKEN"]
    # Для эндпоинта оракула разрешаем также токен оракула
    if request.url.path.endswith("/condition"):
        allowed.extend(["ORACLE_BOOTSTRAP_TOKEN", "AGENT_ORACLE_BOOTSTRAP_TOKEN"])
    return verify_agent_token(
        token=token,
        allowed_env_vars=allowed,
        domain_name="Digital Ruble"
    )


async def verify_oracle_token(token: str = Depends(extract_token)) -> str:
    """FastAPI Dependency для проверки прав оракула при обновлении внешних условий смарт-контрактов."""
    return verify_agent_token(
        token=token,
        allowed_env_vars=["ORACLE_BOOTSTRAP_TOKEN", "AGENT_ORACLE_BOOTSTRAP_TOKEN", "AGENT_DIGITAL_BOOTSTRAP_TOKEN"],
        domain_name="Oracle"
    )
