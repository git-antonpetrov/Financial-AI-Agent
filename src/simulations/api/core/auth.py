import hashlib
import os
import secrets
import time

from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import NameOID
from fastapi import Header, HTTPException, Request, status

from src.simulations.core.crypto.chain_validator import get_chain_validator
from src.simulations.core.crypto.enclave_verifier import get_enclave_verifier
from src.simulations.core.utils.console_logger import log_error, log_info, log_warning


def extract_token_from_headers(
    x_bootstrap_token: str | None = None,
    authorization: str | None = None,
) -> str | None:
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
    x_bootstrap_token: str | None = Header(None, alias="X-Bootstrap-Token"),
    authorization: str | None = Header(None, alias="Authorization"),
) -> str:
    """
    FastAPI зависимость: извлекает токен аутентификации или бросает HTTP 401.
    """
    token = extract_token_from_headers(x_bootstrap_token, authorization)
    if token:
        return token

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=(
            "Отсутствует токен аутентификации (X-Bootstrap-Token или Authorization) "
            "или цифровая подпись Анклава (X-Enclave-Signature)"
        ),
    )


def verify_agent_token(token: str, allowed_env_vars: list[str], domain_name: str) -> str:
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
        log_error(
            "Simulation Auth",
            f"Не сконфигурированы токены доступа для домена '{domain_name}'",
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ошибка конфигурации сервера: токены для сервиса '{domain_name}' не настроены",
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
                    detail=f"Доступ к сервису '{domain_name}' запрещен для данного агента",
                )

    log_warning(
        "Simulation Auth",
        f"Недействительный токен при попытке обращения к '{domain_name}'",
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Недействительный токен аутентификации",
    )


async def check_enclave_or_token_auth(
    request: Request,
    allowed_env_vars: list[str],
    domain_name: str,
) -> str:
    """
    Комплексная проверка аутентификации (Блок 5 схемы arch.txt):
    1. Если переданы заголовки X.509 сертификатов (X-Agent-Cert / X-Cert) и цифровая подпись:
       валидирует цепочку сертификатов (Root CA -> [Enclave CA] -> Leaf Cert) через X509ChainValidator,
       проверяет nonce на Replay Attack, допустимый дрейф времени (±300с) и цифровую подпись запроса.
    2. Если передана подпись Анклава (X-Enclave-Signature),
       проверяет nonce, timestamp и подпись по открытому ключу Enclave_PublicKey.
    3. В остальных случаях проверяет Bootstrap токен агента.
    """
    agent_cert = request.headers.get("X-Agent-Cert") or request.headers.get("X-Cert")
    enclave_cert = request.headers.get("X-Enclave-Cert")
    signature = request.headers.get("X-Signature") or request.headers.get("X-Enclave-Signature")
    nonce = request.headers.get("X-Nonce")
    timestamp = request.headers.get("X-Timestamp")

    # Сценарий 1: Аутентификация по X.509 PKI сертификату агента
    if agent_cert:
        if not (signature and nonce and timestamp):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Для аутентификации по сертификату X.509 требуются заголовки: X-Signature, X-Nonce, X-Timestamp",
            )

        verifier = get_enclave_verifier()
        if not verifier.nonce_cache.record_nonce_if_new(nonce):
            log_warning("Simulation Auth", f"Replay attack на '{domain_name}': nonce '{nonce}' уже использован")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Обнаружена повторная атака (Replay Attack): nonce '{nonce}' уже был использован",
            )

        now = time.time()
        try:
            req_ts = float(timestamp)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Некорректный формат метки времени X-Timestamp",
            )
        if abs(now - req_ts) > 300:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Метка времени запроса устарела (дрейф превышает 300с)",
            )

        chain_val = get_chain_validator()
        leaf_cert = chain_val.parse_certificate(agent_cert)
        if not leaf_cert:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Некорректный формат клиентского сертификата X-Agent-Cert",
            )

        if enclave_cert:
            enc_cert = chain_val.parse_certificate(enclave_cert)
            if not enc_cert:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Некорректный формат сертификата Анклава X-Enclave-Cert",
                )
            is_enc_valid, enc_err = chain_val.validate_certificate_chain(enc_cert)
            if not is_enc_valid:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Промежуточный сертификат Анклава не прошел валидацию: {enc_err}",
                )
            is_leaf_valid, leaf_err = chain_val.validate_certificate_chain(leaf_cert, issuer_ca_input=enc_cert)
            if not is_leaf_valid:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Сертификат агента не прошел валидацию Анклавом: {leaf_err}",
                )
        else:
            is_leaf_valid, leaf_err = chain_val.validate_certificate_chain(leaf_cert)
            if not is_leaf_valid:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Сертификат агента не прошел валидацию Root CA: {leaf_err}",
                )

        # Проверка 5 (Zero-Trust Pipeline): Проверка отзыва сертификата и агента в Redis CRL
        serial_str = str(leaf_cert.serial_number)
        fp_str = f"sha256:{hashlib.sha256(leaf_cert.public_bytes(serialization.Encoding.DER)).hexdigest()}"
        cn_str = None
        for attr in leaf_cert.subject:
            if attr.oid == NameOID.COMMON_NAME:
                cn_str = str(attr.value)
                break

        redis_cli = getattr(verifier.nonce_store, "redis_client", None)
        if redis_cli is not None:
            try:
                if (
                    redis_cli.exists(f"revoked:cert:serial:{serial_str}")
                    or redis_cli.exists(f"revoked:cert:fp:{fp_str}")
                    or (cn_str and redis_cli.exists(f"revoked:agent:{cn_str}"))
                ):
                    log_warning("Simulation Auth", f"Доступ заблокирован: агент {cn_str} или сертификат отозван")
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Доступ заблокирован: агент или сертификат отозван (Certificate has been revoked)",
                    )
            except HTTPException:
                raise
            except Exception as e:
                log_warning("Simulation Auth", f"Ошибка проверки отзыва в Redis: {e}")

        # Проверка 6 (Zero-Trust Pipeline): Проверка роли субъекта (OU / Role validation)
        ou_values = [str(attr.value) for attr in leaf_cert.subject if attr.oid == NameOID.ORGANIZATIONAL_UNIT_NAME]
        valid_roles = {
            "AI Autonomous Financial Agent",
            "bank_operator",
            "Security Operations",
            "bank_service",
            "admin_operator",
            "infrastructure",
            "agent",
        }
        if ou_values and not any(role in valid_roles for role in ou_values):
            log_warning("Simulation Auth", f"Недопустимая роль сертификата: {ou_values}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Недопустимая роль в сертификате (OU={ou_values}) для доступа к банковским операциям",
            )

        body = await request.body()
        canonical_data = chain_val.compute_canonical_digest(
            timestamp=req_ts,
            nonce=nonce,
            body_bytes=body,
            method=request.method,
            path=request.url.path,
        )
        is_sig_valid, sig_err = chain_val.verify_signature(
            payload_bytes=canonical_data,
            signature_b64=signature,
            leaf_cert_input=leaf_cert,
            verify_chain=False,
        )
        if not is_sig_valid:
            canonical_data_compat = chain_val.compute_canonical_digest(
                timestamp=req_ts,
                nonce=nonce,
                body_bytes=body,
            )
            is_sig_valid, sig_err = chain_val.verify_signature(
                payload_bytes=canonical_data_compat,
                signature_b64=signature,
                leaf_cert_input=leaf_cert,
                verify_chain=False,
            )

        if not is_sig_valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Недействительная цифровая подпись запроса агента: {sig_err}",
            )

        request.state.enclave_nonce = nonce
        request.state.authenticated_entity = "pki_agent"
        log_info(
            "Simulation Auth",
            f"Успешная X.509 PKI аутентификация для домена '{domain_name}' (nonce={nonce})",
        )
        return f"pki_agent:{nonce}"

    # Сценарий 2: Аутентификация по цифровой подписи Анклава (X-Enclave-Signature)
    if signature or nonce or timestamp:
        if not (signature and nonce and timestamp):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Для аутентификации Анклава требуются все заголовки: X-Enclave-Signature, X-Nonce, X-Timestamp",
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
            signature_b64=signature,
        )

        if not is_valid:
            if "Replay Attack" in reason or "уже был использован" in reason:
                log_warning("Simulation Auth", f"Replay attack на '{domain_name}': {reason}")
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=reason)
            log_warning(
                "Simulation Auth",
                f"Ошибка верификации Анклава на '{domain_name}': {reason}",
            )
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=reason)

        # Запоминаем проверенный nonce в состоянии запроса для включения в Signed Receipt
        request.state.enclave_nonce = nonce
        request.state.authenticated_entity = "enclave"

        # Если вместе с подписью Анклава передан токен конкретного агента, проводим его доменную валидацию
        optional_token = extract_token_from_headers(
            x_bootstrap_token=request.headers.get("X-Bootstrap-Token"),
            authorization=request.headers.get("Authorization"),
        )
        if optional_token:
            verify_agent_token(
                token=optional_token,
                allowed_env_vars=allowed_env_vars,
                domain_name=domain_name,
            )
            request.state.authenticated_agent = optional_token

        log_info(
            "Simulation Auth",
            f"Успешная верификация подписи Анклава для домена '{domain_name}' (nonce={nonce})",
        )
        return f"enclave:{nonce}"

    # Строгий режим безопасности: подпись Анклава или X.509 сертификат обязательны по умолчанию (SEC-09)
    if os.getenv("REQUIRE_ENCLAVE_SIGNATURE", "true").lower() in ("true", "1", "yes"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Строгий режим безопасности: требуется цифровая подпись Анклава "
                "(X-Enclave-Signature) или X.509 сертификат"
            ),
        )

    # Иначе стандартная проверка Bootstrap токена
    token = extract_token_from_headers(
        x_bootstrap_token=request.headers.get("X-Bootstrap-Token"),
        authorization=request.headers.get("Authorization"),
    )
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Отсутствует токен аутентификации (X-Bootstrap-Token или Authorization) "
                "или цифровая подпись Анклава (X-Enclave-Signature)"
            ),
        )

    return verify_agent_token(token=token, allowed_env_vars=allowed_env_vars, domain_name=domain_name)


async def verify_bank_token(request: Request) -> str:
    """FastAPI Dependency для проверки доступа к API банка (токен или подпись Анклава)."""
    return await check_enclave_or_token_auth(
        request=request,
        allowed_env_vars=["AGENT_BANK_BOOTSTRAP_TOKEN"],
        domain_name="Bank",
    )


async def verify_invest_token(request: Request) -> str:
    """FastAPI Dependency для проверки доступа к API инвестиций (токен или подпись Анклава)."""
    return await check_enclave_or_token_auth(
        request=request,
        allowed_env_vars=["AGENT_INVEST_BOOTSTRAP_TOKEN"],
        domain_name="Invest",
    )


async def verify_digital_token(request: Request) -> str:
    """FastAPI Dependency для проверки доступа к API цифрового рубля (токен или подпись Анклава)."""
    allowed = ["AGENT_DIGITAL_BOOTSTRAP_TOKEN"]
    if request.url.path.endswith("/condition"):
        allowed.extend(["ORACLE_BOOTSTRAP_TOKEN", "AGENT_ORACLE_BOOTSTRAP_TOKEN"])
    return await check_enclave_or_token_auth(request=request, allowed_env_vars=allowed, domain_name="Digital Ruble")


async def verify_oracle_token(request: Request) -> str:
    """FastAPI Dependency для проверки прав оракула при обновлении условий контракта."""
    return await check_enclave_or_token_auth(
        request=request,
        allowed_env_vars=[
            "ORACLE_BOOTSTRAP_TOKEN",
            "AGENT_ORACLE_BOOTSTRAP_TOKEN",
            "AGENT_DIGITAL_BOOTSTRAP_TOKEN",
        ],
        domain_name="Oracle",
    )
