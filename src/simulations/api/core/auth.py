import os
import secrets
from typing import Optional, List
from fastapi import Header, HTTPException, status, Depends, Request

from src.simulations.core.utils.console_logger import log_warning, log_error, log_info
from src.simulations.core.crypto.enclave_verifier import get_enclave_verifier


def extract_token_from_headers(
    x_bootstrap_token: Optional[str] = None,
    authorization: Optional[str] = None,
) -> Optional[str]:
    """
    Извлекает токен аутентификации из переданных заголовков.
    Возвращает строку токена или None.
    """
    if x_bootstrap_token and x_bootstrap_token.strip():
        return x_bootstrap_token.strip()

    if authorization and authorization.strip():
        parts = authorization.strip().split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1]
        elif len(parts) == 1:
            return parts[0]

    return None


def extract_token(
    x_bootstrap_token: Optional[str] = Header(None, alias="X-Bootstrap-Token"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
) -> str:
    """
    FastAPI зависимость: извлекает токен аутентификации или бросает HTTP 401.
    """
    token = extract_token_from_headers(x_bootstrap_token, authorization)
    if token:
        return token

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Отсутствует токен аутентификации (X-Bootstrap-Token или Authorization) или цифровая подпись Анклава (X-Enclave-Signature)"
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


async def check_enclave_or_token_auth(
    request: Request,
    allowed_env_vars: List[str],
    domain_name: str,
) -> str:
    """
    Комплексная проверка аутентификации (Блок 5 схемы arch.txt):
    1. Если переданы заголовки цифровой подписи Анклава (X-Enclave-Signature),
       проверяет nonce, timestamp и подпись по открытому ключу Enclave_PublicKey.
    2. В остальных случаях проверяет Bootstrap токен агента.
    """
    enclave_sig = request.headers.get("X-Enclave-Signature")
    nonce = request.headers.get("X-Nonce")
    timestamp = request.headers.get("X-Timestamp")

    # Если присутствуют заголовки Анклава
    if enclave_sig or nonce or timestamp:
        if not (enclave_sig and nonce and timestamp):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Для аутентификации Анклава требуются все заголовки: X-Enclave-Signature, X-Nonce, X-Timestamp"
            )

        # Читаем тело запроса
        body = await request.body()
        verifier = get_enclave_verifier()

        is_valid, reason = verifier.verify_request(
            method=request.method,
            path=request.url.path,
            timestamp_val=timestamp,
            nonce=nonce,
            body_bytes=body,
            signature_b64=enclave_sig,
        )

        if not is_valid:
            if "Replay Attack" in reason or "уже был использован" in reason:
                log_warning("Simulation Auth", f"Replay attack на '{domain_name}': {reason}")
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=reason
                )
            log_warning("Simulation Auth", f"Ошибка верификации Анклава на '{domain_name}': {reason}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=reason
            )

        # Запоминаем проверенный nonce в состоянии запроса для включения в Signed Receipt
        request.state.enclave_nonce = nonce
        request.state.authenticated_entity = "enclave"
        log_info("Simulation Auth", f"Успешная верификация подписи Анклава для домена '{domain_name}' (nonce={nonce})")
        return f"enclave:{nonce}"

    # Если включен строгий режим обязательной подписи Анклава
    if os.getenv("REQUIRE_ENCLAVE_SIGNATURE", "false").lower() == "true":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Строгий режим безопасности: требуется цифровая подпись Анклава (X-Enclave-Signature)"
        )

    # Иначе стандартная проверка Bootstrap токена
    token = extract_token_from_headers(
        x_bootstrap_token=request.headers.get("X-Bootstrap-Token"),
        authorization=request.headers.get("Authorization"),
    )
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Отсутствует токен аутентификации (X-Bootstrap-Token или Authorization) или цифровая подпись Анклава (X-Enclave-Signature)"
        )

    return verify_agent_token(token=token, allowed_env_vars=allowed_env_vars, domain_name=domain_name)


async def verify_bank_token(request: Request) -> str:
    """FastAPI Dependency для проверки доступа к API банка (токен или подпись Анклава)."""
    return await check_enclave_or_token_auth(
        request=request,
        allowed_env_vars=["AGENT_BANK_BOOTSTRAP_TOKEN"],
        domain_name="Bank"
    )


async def verify_invest_token(request: Request) -> str:
    """FastAPI Dependency для проверки доступа к API инвестиций (токен или подпись Анклава)."""
    return await check_enclave_or_token_auth(
        request=request,
        allowed_env_vars=["AGENT_INVEST_BOOTSTRAP_TOKEN"],
        domain_name="Invest"
    )


async def verify_digital_token(request: Request) -> str:
    """FastAPI Dependency для проверки доступа к API цифрового рубля (токен или подпись Анклава)."""
    allowed = ["AGENT_DIGITAL_BOOTSTRAP_TOKEN"]
    if request.url.path.endswith("/condition"):
        allowed.extend(["ORACLE_BOOTSTRAP_TOKEN", "AGENT_ORACLE_BOOTSTRAP_TOKEN"])
    return await check_enclave_or_token_auth(
        request=request,
        allowed_env_vars=allowed,
        domain_name="Digital Ruble"
    )


async def verify_oracle_token(request: Request) -> str:
    """FastAPI Dependency для проверки прав оракула при обновлении условий контракта."""
    return await check_enclave_or_token_auth(
        request=request,
        allowed_env_vars=["ORACLE_BOOTSTRAP_TOKEN", "AGENT_ORACLE_BOOTSTRAP_TOKEN", "AGENT_DIGITAL_BOOTSTRAP_TOKEN"],
        domain_name="Oracle"
    )
