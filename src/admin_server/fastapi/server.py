import asyncio
import os
import re
import json
import secrets
import tempfile
import anyio
import ipaddress
from datetime import datetime, timezone
from typing import Optional, List
from fastapi import FastAPI, Depends, HTTPException, Request, status, UploadFile, File, Form, Header, Query, Body
# pyrefly: ignore [missing-import]
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
# pyrefly: ignore [missing-import]
import redis
from minio import Minio
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import AsyncSession
import litellm
import jwt

from security import (
    verify_password,
    create_access_token,
    create_refresh_token,
    run_revocation_listener,
    decode_access_token,
    verify_and_rotate_refresh_token,
    get_current_admin,
    verify_agent_jwt,
    verify_totp_code,
    generate_totp_secret,
    get_totp_uri,
    generate_qr_svg,
    set_pending_totp_secret,
    get_pending_totp_secret,
    clear_pending_totp_secret,
    calculate_key_fingerprint,
    revoke_agent_key_in_redis,
    is_agent_key_revoked_in_redis,
    unrevoke_agent_key_in_redis,
    get_minio_sse,
    verify_mtls_client_certificate,
    settings,
    oauth2_scheme,
    Role,
    Permission,
    ROLE_PERMISSIONS,
    ROLE_DESCRIPTIONS,
    CurrentUser,
    get_current_user,
    require_permission,
)
from db.database import engine, Base, get_db, validate_database_env, async_session
from db import schemas, crud, audit
try:
    from src.common.logger import log_info, log_error, log_warning, log_success
    from src.common.llm import default_llm_client, LLMClient
except ImportError:
    from common.logger import log_info, log_error, log_warning, log_success
    from common.llm import default_llm_client, LLMClient

VALID_AGENTS = {"main", "bank", "invest", "digital"}
VALID_ACTIONS = {"upsert", "delete"}

# --- НАСТРОЙКА MINIO ---
MINIO_URL = os.getenv("MINIO_URL", "minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")

minio_client = Minio(
    MINIO_URL,
    access_key=MINIO_ACCESS_KEY,
    secret_key=MINIO_SECRET_KEY,
    secure=settings.MINIO_SECURE
)

# --- НАСТРОЙКА REDIS ---
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")
REDIS_SSL_CA = os.getenv("REDIS_SSL_CA_CERTS", "/certs/ca.crt")

redis_kwargs = {
    "host": REDIS_HOST,
    "port": REDIS_PORT,
    "password": REDIS_PASSWORD,
    "ssl": settings.REDIS_SSL,
    "decode_responses": True,
    "socket_timeout": 2.0,
    "socket_connect_timeout": 2.0,
}
if settings.REDIS_SSL and os.path.exists(REDIS_SSL_CA):
    redis_kwargs["ssl_ca_certs"] = REDIS_SSL_CA
    redis_kwargs["ssl_cert_reqs"] = "required"

redis_client = redis.Redis(**redis_kwargs)


# --- НАСТРОЙКА LLM (через централизованный LLMClient) ---
RAG_DATA_MODEL_NAME = os.getenv("RAG_DATA_MODEL_NAME", "vertex_ai/gemini-3.8-flash")
RAG_DATA_REASONING_EFFORT = os.getenv("RAG_DATA_REASONING_EFFORT", "low")

def get_vertex_api_base(model_name: str) -> str | None:
    return default_llm_client.get_vertex_api_base(model_name)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Валидация обязательных настроек перед стартом сервера
    settings.validate_production_env()
    validate_database_env()

    # Инициализация бакетов MinIO
    if MINIO_ACCESS_KEY and MINIO_SECRET_KEY:
        for agent in VALID_AGENTS:
            bucket_name = f"knowledge-{agent}"
            if not minio_client.bucket_exists(bucket_name):
                minio_client.make_bucket(bucket_name)
                log_info("MinIO", f"Created bucket: {bucket_name}")
    
    # Инициализация таблиц БД
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        log_info("Database", "Database tables created/verified")

    # Загрузка персистентных настроек безопасности из БД (если не заданы через .env)
    try:
        async with async_session() as db:
            persisted_totp = await crud.get_system_setting(db, "ADMIN_TOTP_SECRET")
            if persisted_totp and not settings.ADMIN_TOTP_SECRET:
                settings.ADMIN_TOTP_SECRET = persisted_totp
                log_info("Security", "ADMIN_TOTP_SECRET успешно загружен из зашифрованной базы данных")
    except Exception as e:
        log_warning("Security", f"Не удалось загрузить настройки безопасности из БД: {e}")

    # Фоновая задача мгновенной синхронизации отозванных сертификатов через Redis Pub/Sub
    listener_task = None
    try:
        listener_task = asyncio.create_task(run_revocation_listener(redis_client))
        log_info("Security", "Фоновый слушатель отзывов Redis запущен")
    except Exception as e:
        log_warning("Security", f"Не удалось запустить фоновый слушатель отзывов: {e}")

    yield

    if listener_task:
        listener_task.cancel()
        try:
            await listener_task
        except asyncio.CancelledError:
            pass

app = FastAPI(title="Financial MAS - Admin Server", version="1.0.0", lifespan=lifespan)

# Ограничиваем CORS только необходимыми origin, так как работаем из WebView
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8001", 
        "http://localhost:5173", 
        "tauri://localhost", 
        "https://tauri.localhost",
        "http://tauri.localhost",
        "asset://localhost",
        "https://admin.fin-ai-agent.ru" # Разрешаем CORS для production домена
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "x-bootstrap-token",
        "x-otp-code",
        "x-client-cert-verify",
        "x-client-cert-subject",
        "x-client-cert-issuer",
        "x-client-cert-serial",
        "x-client-cert-fingerprint",
        "x-signature",
        "x-nonce",
        "x-timestamp",
        "x-cert",
        "x-agent-cert",
        "x-enclave-cert",
        "x-server-cert",
        "x-admin-signature",
        "x-server-signature",
        "x-server-key-fingerprint",
        "x-ack-nonce",
    ],
    expose_headers=[
        "x-admin-signature",
        "x-server-signature",
        "x-server-key-fingerprint",
        "x-server-cert",
        "x-ack-nonce",
    ],
)

# Подключение роутера защищенного приема документов RAG (Zero-Trust PKI)
try:
    from routers.rag_documents import router as rag_router
except ImportError:
    try:
        from .routers.rag_documents import router as rag_router
    except ImportError:
        from src.admin_server.fastapi.routers.rag_documents import router as rag_router

app.include_router(rag_router)
for r in rag_router.routes:
    if r not in app.routes:
        app.routes.append(r)

@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.get("/api/auth/public-key")
@app.get("/api/v1/auth/public-key")
async def get_server_public_key():
    """Возвращает публичный ключ и сертификат сервера для проверки подписей квитанций."""
    return {
        "algorithm": settings.ALGORITHM,
        "public_key": settings.PUBLIC_KEY,
        "certificate": settings.SERVER_CERT or None,
        "fingerprint": calculate_key_fingerprint(settings.PUBLIC_KEY),
    }


def get_client_ip(request: Request) -> str:
    """
    Извлекает реальный IP-адрес клиента с защитой от спуфинга X-Forwarded-For.
    За обратным прокси Caddy доверенный IP клиента добавляется в конец списка X-Forwarded-For.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        ips = [ip.strip() for ip in forwarded.split(",") if ip.strip()]
        if ips:
            candidate = ips[-1]
            try:
                ipaddress.ip_address(candidate)
                return candidate
            except ValueError:
                pass
    if request.client and request.client.host:
        try:
            ipaddress.ip_address(request.client.host)
            return request.client.host
        except ValueError:
            pass
    return "127.0.0.1"

def check_rate_limit(key: str, max_requests: int = 15, window_seconds: int = 60) -> bool:
    """
    Проверяет лимит запросов через Redis.
    Возвращает True если лимит не превышен, False если превышен.
    """
    if not redis_client:
        return True
    try:
        current = redis_client.incr(key)
        if current == 1:
            redis_client.expire(key, window_seconds)
        return current <= max_requests
    except Exception as e:
        log_warning("RateLimit", f"Failed to check rate limit in Redis: {e}")
        return True

# --- МАРШРУТЫ: АВТОРИЗАЦИЯ ---
@app.post("/login", response_model=schemas.TokenResponse)
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    otp_code: Optional[str] = Form(default=None),
    x_otp_code: Optional[str] = Header(default=None, alias="X-OTP-Code"),
    db: AsyncSession = Depends(get_db)
):
    """Выполняет двухфакторную аутентификацию администратора с выдачей пары JWT-токенов."""
    client_ip = get_client_ip(request)
    origin = request.headers.get("origin")
    log_info("Auth", f"Запрос на логин, IP: {client_ip}, Origin: {origin}")

    # Защита от DoS/bcrypt CPU exhaustion: не более 15 обращений к /login в минуту с одного IP
    if not check_rate_limit(f"ratelimit:login:{client_ip}", max_requests=15, window_seconds=60):
        log_warning("Auth", f"Rate limit exceeded for login from IP: {client_ip}")
        await audit.log_audit_event(db, actor=form_data.username, action="AUTH_RATELIMIT_EXCEEDED", status="WARNING", actor_ip=client_ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Please try again later."
        )

    # Защита от подбора пароля: не более 5 неудачных попыток в течение 5 минут
    failed_key = f"ratelimit:login_failed:{client_ip}"
    try:
        failed_count = int(redis_client.get(failed_key) or 0)
        if failed_count >= 5:
            log_warning("Auth", f"Account lockout: IP {client_ip} has 5+ failed login attempts")
            await audit.log_audit_event(db, actor=form_data.username, action="AUTH_LOCKOUT", status="WARNING", actor_ip=client_ip, details="Account lockout (5+ failed attempts)")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed login attempts. IP temporarily blocked for 5 minutes."
            )
    except HTTPException:
        raise
    except Exception as e:
        log_warning("Auth", f"Failed to check failed attempts in Redis: {e}")

    if not secrets.compare_digest(form_data.username, "admin"):
        try:
            curr = redis_client.incr(failed_key)
            if curr == 1:
                redis_client.expire(failed_key, 300)
        except Exception:
            pass
        log_warning("Auth", f"Failed login attempt for user: {form_data.username} from IP: {client_ip}")
        await audit.log_audit_event(db, actor=form_data.username, action="AUTH_LOGIN_FAILED", status="FAILURE", actor_ip=client_ip, details="Unknown username")
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    
    if not settings.ADMIN_PASSWORD_HASH:
        log_error("Auth", "Server not configured: ADMIN_PASSWORD_HASH missing")
        raise HTTPException(status_code=500, detail="Server not configured: ADMIN_PASSWORD_HASH missing")

    if not verify_password(form_data.password, settings.ADMIN_PASSWORD_HASH):
        try:
            curr = redis_client.incr(failed_key)
            if curr == 1:
                redis_client.expire(failed_key, 300)
        except Exception:
            pass
        log_warning("Auth", f"Failed login attempt (bad password) from IP: {client_ip}")
        await audit.log_audit_event(db, actor=form_data.username, action="AUTH_LOGIN_FAILED", status="FAILURE", actor_ip=client_ip, details="Incorrect password")
        raise HTTPException(status_code=400, detail="Incorrect username or password")

    # ВТОРОЙ ФАКТОР: Проверка одноразового TOTP-кода (RFC 6238)
    totp_code = (otp_code or x_otp_code or "").strip()
    pending_secret = get_pending_totp_secret(form_data.username, redis_conn=redis_client)
    is_2fa_required = settings.is_2fa_enabled()

    if is_2fa_required or totp_code:
        if not totp_code:
            log_warning("Auth", f"Login attempt without required 2FA code from IP: {client_ip}")
            await audit.log_audit_event(db, actor=form_data.username, action="AUTH_2FA_REQUIRED", status="WARNING", actor_ip=client_ip)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="2FA code required",
                headers={"WWW-Authenticate": "Bearer", "X-2FA-Required": "true"}
            )

        # Защита от Replay Attack: предотвращение повторного применения одного и того же кода
        replay_key = f"totp_used:{form_data.username}:{totp_code}"
        if redis_client:
            try:
                if redis_client.exists(replay_key):
                    log_warning("Auth", f"Replay attack detected: TOTP code {totp_code} reused from IP: {client_ip}")
                    await audit.log_audit_event(db, actor=form_data.username, action="AUTH_2FA_REPLAY_ATTACK", status="FAILURE", actor_ip=client_ip, details=f"Replayed TOTP: {totp_code}")
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="TOTP code has already been used. Please wait for the next code.",
                        headers={"WWW-Authenticate": "Bearer"}
                    )
            except HTTPException:
                raise
            except Exception as e:
                log_warning("Auth", f"Redis TOTP replay check error: {e}")

        # Целевой секрет для валидации: постоянный ключ сервера либо pending-ключ онбординга
        target_secret = settings.ADMIN_TOTP_SECRET or pending_secret

        if not target_secret or not verify_totp_code(target_secret, totp_code):
            try:
                curr = redis_client.incr(failed_key)
                if curr == 1:
                    redis_client.expire(failed_key, 300)
            except Exception:
                pass
            log_warning("Auth", f"Failed login attempt (bad 2FA TOTP code) from IP: {client_ip}")
            await audit.log_audit_event(db, actor=form_data.username, action="AUTH_2FA_FAILED", status="FAILURE", actor_ip=client_ip, details="Invalid 2FA TOTP code")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid 2FA TOTP code",
                headers={"WWW-Authenticate": "Bearer"}
            )

        # Если валидация прошла успешно и это был первичный онбординг (pending_secret):
        if pending_secret and (not settings.ADMIN_TOTP_SECRET or target_secret == pending_secret):
            # Персистентное сохранение в зашифрованное хранилище БД (AES-256-GCM)
            try:
                await crud.set_system_setting(
                    db,
                    key="ADMIN_TOTP_SECRET",
                    value=pending_secret,
                    description="Admin TOTP 2FA secret (AES-256-GCM encrypted)"
                )
            except Exception as e:
                log_warning("Auth", f"Не удалось персистентно сохранить 2FA секрет в БД (fallback): {e}")
            settings.ADMIN_TOTP_SECRET = pending_secret
            clear_pending_totp_secret(form_data.username, redis_conn=redis_client)
            await audit.log_audit_event(
                db,
                actor=form_data.username,
                action="AUTH_2FA_ONBOARDED_PERSISTED",
                status="SUCCESS",
                actor_ip=client_ip,
                details="2FA successfully onboarded and persisted to encrypted storage"
            )

        # Фиксируем использование одноразового пароля на 90 секунд в Redis и отмечаем завершение привязки 2FA
        if redis_client:
            try:
                redis_client.setex(replay_key, 90, "used")
                redis_client.set(f"auth:totp_enrolled:{form_data.username}", "1")
            except Exception:
                pass

    # Сбрасываем счетчик неудачных попыток при успешной авторизации
    try:
        redis_client.delete(failed_key)
    except Exception:
        pass

    # Для модели «1 сервер — 1 админ» токен администратора всегда выпускается с ролью SUPERADMIN
    user_role = Role.SUPERADMIN

    access_token = create_access_token(data={"sub": form_data.username}, role=user_role)
    refresh_token = create_refresh_token(data={"sub": form_data.username}, role=user_role)
    log_success("Auth", f"Successful login with 2FA verification from IP: {client_ip}, role: {user_role.value}")
    await audit.log_audit_event(
        db,
        actor=form_data.username,
        action="AUTH_LOGIN_SUCCESS",
        status="SUCCESS",
        actor_ip=client_ip,
        details={"role": user_role.value}
    )
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "refresh_token": refresh_token,
        "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }

@app.get("/api/auth/me", response_model=schemas.CurrentUserResponse)
async def get_current_user_profile(current_user: CurrentUser = Depends(get_current_user)):
    """Возвращает профиль текущего авторизованного пользователя, его роль и список разрешений."""
    return schemas.CurrentUserResponse(
        username=current_user.username,
        role=current_user.role.value,
        permissions=sorted([p.value for p in current_user.permissions])
    )

@app.get("/api/auth/roles", response_model=schemas.RoleMatrixResponse)
async def get_role_matrix(current_user: CurrentUser = Depends(get_current_user)):
    """Возвращает матрицу доступных ролей и всех назначенных им разрешений."""
    roles_info = [
        schemas.RoleInfoResponse(
            role=r.value,
            description=ROLE_DESCRIPTIONS.get(r, ""),
            permissions=sorted([p.value for p in ROLE_PERMISSIONS.get(r, set())])
        )
        for r in Role
    ]
    return schemas.RoleMatrixResponse(
        roles=roles_info,
        all_permissions=sorted([p.value for p in Permission])
    )

async def check_totp_enrolled(db: AsyncSession) -> bool:
    """Проверяет, завершена ли первичная привязка 2FA хотя бы одним успешным входом."""
    if redis_client:
        try:
            val = redis_client.get("auth:totp_enrolled:admin")
            if val is not None:
                return (str(val) == "1")
        except Exception:
            pass
    try:
        db_secret = await crud.get_system_setting(db, "ADMIN_TOTP_SECRET")
        if db_secret:
            if redis_client:
                try:
                    redis_client.set("auth:totp_enrolled:admin", "1")
                except Exception:
                    pass
            return True
    except Exception:
        pass
    try:
        from db.models import AuditLog
        from sqlalchemy import select
        res = await db.execute(
            select(AuditLog.id).where(AuditLog.action.in_(["AUTH_LOGIN_SUCCESS", "AUTH_2FA_ONBOARDED_PERSISTED"])).limit(1)
        )
        has_login = res.scalar_one_or_none() is not None
        if has_login and redis_client:
            try:
                redis_client.set("auth:totp_enrolled:admin", "1")
            except Exception:
                pass
        return has_login
    except Exception:
        pass
    return False

@app.get("/api/auth/2fa/status")
async def get_2fa_status(db: AsyncSession = Depends(get_db)):
    """Возвращает статус активности обязательной двухфакторной аутентификации 2FA."""
    enrolled = await check_totp_enrolled(db)
    return {
        "enabled": settings.is_2fa_enabled(),
        "enrolled": enrolled,
        "method": "TOTP_RFC_6238" if settings.is_2fa_enabled() else "none"
    }

@app.post("/api/auth/2fa/pair", response_model=schemas.TwoFactorPairResponse)
async def pair_2fa(
    request: Request,
    payload: schemas.TwoFactorPairRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Эндпоинт первоначальной настройки и привязки 2FA аутентификатора из десктопного клиента.
    Требует корректный мастер-пароль администратора.
    Разрешен, если 2FA еще не была подтверждена (initial enrollment)
    ЛИБО если передан валидный токен восстановления / bootstrap-токен.
    Защищен ограничением частоты запросов (rate limiting) и аудитом.
    """
    client_ip = request.client.host if request.client else "unknown"
    failed_key = f"failed_logins:{client_ip}"

    # Защита от брутфорса
    if redis_client:
        try:
            failed_attempts = int(redis_client.get(failed_key) or 0)
            if failed_attempts >= 5:
                log_warning("Auth", f"Too many failed login/2fa-pair attempts from IP: {client_ip}")
                await audit.log_audit_event(
                    db,
                    actor=payload.username,
                    action="AUTH_RATE_LIMIT_BLOCKED",
                    status="FAILURE",
                    actor_ip=client_ip
                )
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many failed attempts. Please try again later."
                )
        except HTTPException:
            raise
        except Exception:
            pass

    if not secrets.compare_digest(payload.username, "admin"):
        if redis_client:
            try:
                curr = redis_client.incr(failed_key)
                if curr == 1:
                    redis_client.expire(failed_key, 300)
            except Exception:
                pass
        log_warning("Auth", f"Failed 2FA pair attempt for user: {payload.username} from IP: {client_ip}")
        await audit.log_audit_event(
            db,
            actor=payload.username,
            action="AUTH_2FA_PAIR_FAILED",
            status="FAILURE",
            actor_ip=client_ip,
            details="Unknown username"
        )
        raise HTTPException(status_code=400, detail="Incorrect username or password")

    if not settings.ADMIN_PASSWORD_HASH:
        log_error("Auth", "Server not configured: ADMIN_PASSWORD_HASH missing")
        raise HTTPException(status_code=500, detail="Server not configured: ADMIN_PASSWORD_HASH missing")

    if not verify_password(payload.password, settings.ADMIN_PASSWORD_HASH):
        if redis_client:
            try:
                curr = redis_client.incr(failed_key)
                if curr == 1:
                    redis_client.expire(failed_key, 300)
            except Exception:
                pass
        log_warning("Auth", f"Failed 2FA pair attempt (bad password) from IP: {client_ip}")
        await audit.log_audit_event(
            db,
            actor=payload.username,
            action="AUTH_2FA_PAIR_FAILED",
            status="FAILURE",
            actor_ip=client_ip,
            details="Incorrect password"
        )
        raise HTTPException(status_code=400, detail="Incorrect username or password")

    is_enrolled = await check_totp_enrolled(db)

    # Проверка recovery / bootstrap токена администратора (полная изоляция от токенов агентов)
    expected_setup_token = os.getenv("ADMIN_SETUP_TOKEN", "").strip()
    has_valid_setup_token = bool(
        payload.setup_token
        and expected_setup_token
        and secrets.compare_digest(payload.setup_token.strip(), expected_setup_token)
    )

    if is_enrolled and not has_valid_setup_token:
        log_warning("Auth", f"2FA pair request rejected: already enrolled for IP: {client_ip}")
        await audit.log_audit_event(
            db,
            actor=payload.username,
            action="AUTH_2FA_PAIR_REJECTED",
            status="FAILURE",
            actor_ip=client_ip,
            details="2FA already enrolled"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="2FA is already enrolled. Please log in using your authenticator code, or use a recovery setup token."
        )

    # Сбрасываем счетчик неудачных попыток при успешной проверке пароля
    if redis_client:
        try:
            redis_client.delete(failed_key)
        except Exception:
            pass

    # 1. Если ADMIN_TOTP_SECRET уже настроен (в .env или в БД):
    #    - Отдаем существующий постоянный секрет сервера (полная обратная совместимость!).
    # 2. Если секрет еще не настроен (новый инстанс):
    #    - Генерируем новый 160-битный секрет и сохраняем его в pending до подтверждения кодом.
    if settings.ADMIN_TOTP_SECRET:
        secret = settings.ADMIN_TOTP_SECRET
    else:
        secret = generate_totp_secret()
        set_pending_totp_secret(payload.username, secret, ttl_seconds=300, redis_conn=redis_client)

    uri = get_totp_uri(secret, username=payload.username)
    qr_svg = generate_qr_svg(uri)

    await audit.log_audit_event(
        db,
        actor=payload.username,
        action="AUTH_2FA_PAIR_PROVISIONED",
        status="SUCCESS",
        actor_ip=client_ip,
        details={"issuer": "Financial-AI-Agent", "initial_enrollment": not is_enrolled}
    )

    return schemas.TwoFactorPairResponse(
        status="ok",
        secret=secret,
        provisioning_uri=uri,
        qr_svg=qr_svg,
        issuer="Financial-AI-Agent",
        is_enrolled=is_enrolled
    )

@app.get("/api/auth/2fa/setup")
async def get_2fa_setup(current_admin: CurrentUser = Depends(require_permission(Permission.SECURITY_MANAGE))):
    """Возвращает данные подключения 2FA (секрет, URI, SVG QR-код) для текущего или нового ключа."""
    secret = settings.ADMIN_TOTP_SECRET or generate_totp_secret()
    uri = get_totp_uri(secret, username=current_admin.username)
    qr_svg = generate_qr_svg(uri)
    return {
        "secret": secret,
        "provisioning_uri": uri,
        "issuer": "Financial-AI-Agent",
        "qr_svg": qr_svg,
        "is_configured": bool(settings.ADMIN_TOTP_SECRET)
    }

@app.post("/api/auth/2fa/setup")
async def setup_2fa(current_admin: CurrentUser = Depends(require_permission(Permission.SECURITY_MANAGE))):
    """Генерирует новый секрет TOTP, URI и SVG QR-код для перенастройки приложения аутентификации."""
    new_secret = generate_totp_secret()
    uri = get_totp_uri(new_secret, username=current_admin.username)
    qr_svg = generate_qr_svg(uri)
    return {
        "secret": new_secret,
        "provisioning_uri": uri,
        "issuer": "Financial-AI-Agent",
        "qr_svg": qr_svg,
        "is_configured": False
    }

@app.post("/api/auth/2fa/verify-test")
async def verify_2fa_test(
    code: str = Form(...),
    secret: Optional[str] = Form(default=None),
    current_admin: CurrentUser = Depends(require_permission(Permission.SECURITY_MANAGE))
):
    """Проверяет одноразовый 6-значный код при привязке аутентификатора."""
    check_secret = (secret or "").strip() or settings.ADMIN_TOTP_SECRET
    if not check_secret:
        raise HTTPException(status_code=400, detail="No TOTP secret configured to verify")
    if not verify_totp_code(check_secret, code):
        raise HTTPException(status_code=400, detail="Invalid verification code")
    return {"status": "ok", "message": "2FA TOTP code verified successfully"}

@app.get("/api/auth/mtls/status")
async def get_mtls_status(cert_info: dict = Depends(verify_mtls_client_certificate)):
    """
    Возвращает информацию о текущем статусе проверки клиентского TLS-сертификата (mTLS).
    """
    return {
        "mtls_required": settings.REQUIRE_MTLS,
        "verified": cert_info["verified"],
        "is_present": cert_info["is_present"],
        "common_name": cert_info["common_name"],
        "subject": cert_info["subject"],
        "issuer": cert_info["issuer"],
        "serial": cert_info["serial"],
        "fingerprint": cert_info["fingerprint"],
        "raw_verify_status": cert_info["raw_verify_status"],
    }

@app.post("/api/auth/refresh", response_model=schemas.TokenResponse)
async def refresh_tokens(
    req: schemas.RefreshTokenRequest,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    Обновление пары access/refresh токенов по схеме Refresh Token Rotation (RTR).
    Старый refresh-токен немедленно инвалидируется в Redis.
    При попытке повторного использования возвращается 401 Unauthorized.
    """
    new_access, new_refresh = verify_and_rotate_refresh_token(req.refresh_token)
    await audit.log_audit_event(db, actor="admin", action="AUTH_TOKEN_REFRESH", status="SUCCESS", actor_ip=get_client_ip(request))
    return {
        "access_token": new_access,
        "token_type": "bearer",
        "refresh_token": new_refresh,
        "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }

@app.post("/api/auth/logout")
async def logout(
    request: Request,
    req: Optional[schemas.LogoutRequest] = Body(default=None),
    token: str = Depends(oauth2_scheme),
    current_admin: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """
    Отзыв токенов. Добавляет jti и токен доступа (а также refresh_token при наличии) в Redis Blacklist.
    """
    from security import redis_blacklist
    if redis_blacklist:
        try:
            now_ts = int(datetime.now(timezone.utc).timestamp())
            payload = decode_access_token(token)
            jti = payload.get("jti")
            exp = payload.get("exp")
            ttl = max(1, exp - now_ts) if exp else settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60

            if jti:
                redis_blacklist.setex(f"blacklist:{jti}", ttl, "revoked")
            redis_blacklist.setex(f"blacklist:{token}", ttl, "revoked")

            if req and req.refresh_token:
                try:
                    ref_payload = decode_access_token(req.refresh_token)
                    ref_jti = ref_payload.get("jti")
                    ref_exp = ref_payload.get("exp")
                    ref_ttl = max(1, ref_exp - now_ts) if ref_exp else settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400
                    if ref_jti:
                        redis_blacklist.setex(f"blacklist:{ref_jti}", ref_ttl, "revoked")
                    redis_blacklist.setex(f"blacklist:{req.refresh_token}", ref_ttl, "revoked")
                except Exception:
                    pass
        except Exception as e:
            log_error("Auth", f"Failed to blacklist token in Redis: {str(e)}")
            
    log_success("Auth", "Successful logout, token(s) revoked in Redis")
    await audit.log_audit_event(db, actor=current_admin, action="AUTH_LOGOUT", status="SUCCESS", actor_ip=get_client_ip(request))
    return {"status": "ok", "message": "Token revoked successfully"}

@app.get("/api/config/contentai", response_model=schemas.ContentAiConfigResponse)
async def get_contentai_config(current_admin: CurrentUser = Depends(require_permission(Permission.DOCUMENTS_READ))):
    """Возвращает учетные данные сервиса Content AI для локального sidecar клиента."""
    return schemas.ContentAiConfigResponse(
        username=os.getenv("CONTENTAI_USERNAME", ""),
        password=os.getenv("CONTENTAI_PASSWORD", ""),
        api_uri=os.getenv("CONTENT_AI_API_URI", "")
    )

# --- МАРШРУТЫ: ПРОВЕРКА ДОКУМЕНТОВ ---
@app.post("/api/documents/check/md5", response_model=schemas.CheckHashResponse)
async def check_md5(
    req: schemas.CheckHashRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: CurrentUser = Depends(require_permission(Permission.DOCUMENTS_READ))
):
    log_info("Check MD5", f"Checking hash for file: {req.filename}")
    status_str = await crud.check_md5(db, req.file_hash, req.filename, req.agent_name)
    return schemas.CheckHashResponse(status=status_str)

@app.post("/api/documents/check/date", response_model=schemas.CheckDateResponse)
async def check_date(
    req: schemas.CheckDateRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: CurrentUser = Depends(require_permission(Permission.DOCUMENTS_READ))
):
    log_info("Check Date", f"Checking date for system_name: {req.system_name}")
    status_str = await crud.check_date_version(
        db, req.system_name, req.short_name, req.file_hash, req.filename, req.agent_name
    )
    return schemas.CheckDateResponse(status=status_str)



# --- МАРШРУТЫ: ЗАЯВКИ АГЕНТОВ ---
@app.get("/api/agent-requests", response_model=list[schemas.AgentRequestResponse])
async def read_agent_requests(
    skip: int = Query(default=0, ge=0, description="Количество пропускаемых записей"),
    limit: int = Query(default=50, ge=1, le=100, description="Количество возвращаемых записей (максимум 100)"),
    db: AsyncSession = Depends(get_db),
    current_admin: CurrentUser = Depends(require_permission(Permission.REQUESTS_READ))
):
    """
    Возвращает список заявок от агентов с поддержкой пагинации через skip и limit.
    """
    requests = await crud.get_agent_requests(db, skip=skip, limit=limit)
    return requests

@app.post("/api/agents/register", response_model=schemas.AgentRegisterResponse)
async def register_agent(
    req: schemas.AgentRegisterRequest,
    x_bootstrap_token: str = Header(...),
    db: AsyncSession = Depends(get_db)
):
    """
    Регистрация или плановая ротация публичного ключа агента. Защищено Bootstrap-токеном.
    """
    expected_token = settings.get_bootstrap_token(req.agent_name)
    if not expected_token or not secrets.compare_digest(x_bootstrap_token, expected_token):
        log_warning("Agent Registration", f"Invalid bootstrap token for {req.agent_name}")
        raise HTTPException(status_code=403, detail="Invalid bootstrap token")
    
    try:
        await crud.register_agent(db, req.agent_name, req.public_key)
    except ValueError as e:
        log_warning("Agent Registration", str(e))
        raise HTTPException(status_code=409, detail=str(e))
        
    log_success("Agent Registration", f"Agent {req.agent_name} registered/rotated successfully with public key")
    return schemas.AgentRegisterResponse(status="success", message="Public key registered/rotated successfully")

@app.post("/api/agents/rotate", response_model=schemas.AgentActionResponse)
async def rotate_agent_key_endpoint(
    req: schemas.AgentRotateKeyRequest,
    x_bootstrap_token: str = Header(...),
    db: AsyncSession = Depends(get_db)
):
    """
    Эндпоинт плановой ротации ключа со стороны агента (с валидацией bootstrap-токена).
    """
    expected_token = settings.get_bootstrap_token(req.agent_name)
    if not expected_token or not secrets.compare_digest(x_bootstrap_token, expected_token):
        log_warning("Agent Key Rotation", f"Invalid bootstrap token for {req.agent_name}")
        raise HTTPException(status_code=403, detail="Invalid bootstrap token")

    try:
        _, new_key = await crud.rotate_agent_key(db, req.agent_name, req.new_public_key, req.ttl_days or 90)
    except ValueError as e:
        log_warning("Agent Key Rotation", str(e))
        raise HTTPException(status_code=400, detail=str(e))

    log_success("Agent Key Rotation", f"Successfully rotated key for {req.agent_name}, new kid: {new_key.kid}")
    return schemas.AgentActionResponse(
        status="success",
        message="Agent key rotated successfully",
        agent_name=req.agent_name,
        key_id=new_key.kid
    )

# --- АДМИНИСТРАТИВНОЕ УПРАВЛЕНИЕ КЛЮЧАМИ И ЖИЗНЕННЫМ ЦИКЛОМ АГЕНТОВ ---
@app.get("/api/agents", response_model=List[schemas.AgentDetailResponse])
async def list_agents(
    db: AsyncSession = Depends(get_db),
    current_admin: CurrentUser = Depends(require_permission(Permission.AGENTS_READ))
):
    """Возвращает список всех зарегистрированных агентов с полной историей ключей и статусами."""
    agents = await crud.list_all_agents(db)
    response = []
    for a in agents:
        keys_info = []
        active_kid = None
        for k in a.keys:
            if k.status == "active" and not k.is_revoked:
                active_kid = k.kid
            keys_info.append(schemas.AgentKeyInfo(
                kid=k.kid,
                status=k.status,
                is_revoked=k.is_revoked,
                revocation_reason=k.revocation_reason,
                created_at=k.created_at,
                expires_at=k.expires_at,
                revoked_at=k.revoked_at,
                fingerprint=calculate_key_fingerprint(k.public_key)
            ))
        response.append(schemas.AgentDetailResponse(
            id=a.id,
            name=a.name,
            status=a.status,
            registered_at=a.registered_at,
            active_key_id=active_kid,
            keys=keys_info
        ))
    return response

@app.get("/api/agents/{agent_name}/keys", response_model=List[schemas.AgentKeyInfo])
async def get_agent_keys(
    agent_name: str,
    db: AsyncSession = Depends(get_db),
    current_admin: CurrentUser = Depends(require_permission(Permission.AGENTS_READ))
):
    """Возвращает историю ключей конкретного агента."""
    keys = await crud.list_agent_keys(db, agent_name)
    return [
        schemas.AgentKeyInfo(
            kid=k.kid,
            status=k.status,
            is_revoked=k.is_revoked,
            revocation_reason=k.revocation_reason,
            created_at=k.created_at,
            expires_at=k.expires_at,
            revoked_at=k.revoked_at,
            fingerprint=calculate_key_fingerprint(k.public_key)
        )
        for k in keys
    ]

@app.post("/api/agents/{agent_name}/rotate", response_model=schemas.AgentActionResponse)
async def admin_rotate_agent_key(
    agent_name: str,
    req: schemas.AgentRotateKeyRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: CurrentUser = Depends(require_permission(Permission.KEYS_ROTATE))
):
    """Административная принудительная ротация открытого ключа агента."""
    if agent_name != req.agent_name:
        raise HTTPException(status_code=400, detail="Path agent_name does not match request body")
    try:
        _, new_key = await crud.rotate_agent_key(db, agent_name, req.new_public_key, req.ttl_days or 90)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    log_success("Admin Agent Key", f"Admin {current_admin} rotated key for {agent_name}, new kid: {new_key.kid}")
    await audit.log_audit_event(
        db,
        actor=current_admin,
        action="KEY_ROTATED",
        status="SUCCESS",
        resource=f"agent:{agent_name}",
        details={"kid": new_key.kid, "ttl_days": req.ttl_days or 90}
    )
    return schemas.AgentActionResponse(
        status="success",
        message="Key rotated successfully by administrator",
        agent_name=agent_name,
        key_id=new_key.kid
    )

@app.post("/api/agents/{agent_name}/revoke", response_model=schemas.AgentActionResponse)
async def admin_revoke_agent_key(
    agent_name: str,
    req: schemas.AgentRevokeKeyRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: CurrentUser = Depends(require_permission(Permission.KEYS_REVOKE))
):
    """Отзыв скомпрометированного ключа агента администратором с мгновенной инвалидацией в Redis."""
    try:
        revoked_keys = await crud.revoke_agent_key(db, agent_name, req.kid, req.reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    for k in revoked_keys:
        revoke_agent_key_in_redis(agent_name, k.kid, redis_conn=redis_client)

    log_warning("Admin Agent Key", f"Admin {current_admin} revoked {len(revoked_keys)} key(s) for agent {agent_name}, reason: {req.reason}")
    await audit.log_audit_event(
        db,
        actor=current_admin,
        action="KEY_REVOKED",
        status="WARNING",
        resource=f"agent:{agent_name}",
        details={"kid": req.kid or "all", "revoked_count": len(revoked_keys), "reason": req.reason}
    )
    return schemas.AgentActionResponse(
        status="success",
        message=f"Revoked {len(revoked_keys)} key(s) successfully",
        agent_name=agent_name,
        key_id=req.kid
    )

@app.post("/api/agents/{agent_name}/suspend", response_model=schemas.AgentActionResponse)
async def admin_suspend_agent(
    agent_name: str,
    db: AsyncSession = Depends(get_db),
    current_admin: CurrentUser = Depends(require_permission(Permission.AGENTS_MANAGE))
):
    """Аварийная блокировка агента (Emergency Kill Switch) администратором."""
    try:
        agent = await crud.suspend_agent(db, agent_name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    revoke_agent_key_in_redis(agent_name, redis_conn=redis_client)
    log_warning("Admin Agent Key", f"Admin {current_admin} SUSPENDED agent {agent_name}")
    await audit.log_audit_event(
        db,
        actor=current_admin,
        action="AGENT_SUSPENDED",
        status="WARNING",
        resource=f"agent:{agent_name}",
        details="Emergency kill switch triggered"
    )
    return schemas.AgentActionResponse(
        status="success",
        message=f"Agent {agent_name} suspended successfully",
        agent_name=agent_name
    )

@app.post("/api/agents/{agent_name}/reactivate", response_model=schemas.AgentActionResponse)
async def admin_reactivate_agent(
    agent_name: str,
    db: AsyncSession = Depends(get_db),
    current_admin: CurrentUser = Depends(require_permission(Permission.AGENTS_MANAGE))
):
    """Разблокировка ранее приостановленного агента администратором."""
    try:
        agent = await crud.reactivate_agent(db, agent_name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    # Снимаем блокировку в локальной памяти и Redis при реактивации
    unrevoke_agent_key_in_redis(agent_name, redis_conn=redis_client)
    log_success("Admin Agent Key", f"Admin {current_admin} reactivated agent {agent_name}")
    await audit.log_audit_event(
        db,
        actor=current_admin,
        action="AGENT_REACTIVATED",
        status="SUCCESS",
        resource=f"agent:{agent_name}"
    )
    return schemas.AgentActionResponse(
        status="success",
        message=f"Agent {agent_name} reactivated successfully",
        agent_name=agent_name
    )

@app.post("/api/agent_requests", response_model=schemas.AgentRequestResponse)
@app.post("/api/agent-requests", response_model=schemas.AgentRequestResponse)
async def create_agent_request(
    req: schemas.AgentRequestJWT,
    db: AsyncSession = Depends(get_db)
):
    """
    Создание заявки на документ от агента.
    Проверяет валидность агента, статус отзыва/блокировки в Redis и БД,
    соответствие kid и криптографическую подпись RS256.
    """
    try:
        # Декодируем claims без проверки подписи только для определения имени агента
        unverified_payload = jwt.decode(req.token, options={"verify_signature": False})
        agent_name = unverified_payload.get("agent_name")
        unverified_headers = jwt.get_unverified_header(req.token)
        token_kid = unverified_headers.get("kid")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JWT format")

    if not agent_name:
        raise HTTPException(status_code=400, detail="Missing 'agent_name' claim in JWT")

    # 1. Мгновенная проверка в черном списке Redis (Revocation Cache)
    if is_agent_key_revoked_in_redis(agent_name, token_kid):
        log_warning("Agent Request", f"Agent {agent_name} or key {token_kid} is blacklisted in Redis")
        raise HTTPException(status_code=403, detail="Agent public key has been revoked")

    # 2. Проверка статуса агента в БД
    agent = await crud.get_agent(db, agent_name)
    if not agent:
        log_warning("Agent Request", f"Agent {agent_name} is not registered")
        raise HTTPException(status_code=404, detail="Agent not registered")

    if agent.status in ("suspended", "revoked"):
        log_warning("Agent Request", f"Agent {agent_name} has status '{agent.status}'")
        raise HTTPException(status_code=403, detail=f"Agent {agent_name} is currently {agent.status}")

    # 3. Поиск активного неотзозванного и непросроченного ключа
    active_key = await crud.get_active_agent_key(db, agent_name, token_kid)
    if not active_key:
        all_keys = await crud.list_agent_keys(db, agent_name)
        matching_key = next((k for k in all_keys if k.kid == token_kid), None) if token_kid else None
        if matching_key and matching_key.is_revoked:
            log_warning("Agent Request", f"Key {token_kid} for agent {agent_name} is revoked")
            raise HTTPException(status_code=403, detail="Agent public key has been revoked")
        if matching_key and matching_key.status == "expired":
            log_warning("Agent Request", f"Key {token_kid} for agent {agent_name} has expired")
            raise HTTPException(status_code=403, detail="Agent public key has expired (rotation required)")
        log_warning("Agent Request", f"No valid active public key found for agent {agent_name}")
        raise HTTPException(status_code=403, detail="No active or valid public key found for agent")

    # 4. Проверка криптографической подписи RS256
    verified_payload = verify_agent_jwt(req.token, active_key.public_key, expected_kid=active_key.kid if token_kid else None)
    if not verified_payload:
        log_warning("Agent Request", f"Invalid RSA signature from agent {agent_name}")
        raise HTTPException(status_code=403, detail="Invalid RSA signature")

    # 5. Извлекаем все поля заявки строго из верифицированного payload
    verified_agent_name = verified_payload.get("agent_name")
    if verified_agent_name != agent_name:
        log_warning("Agent Request", f"Agent name mismatch between unverified ({agent_name}) and verified ({verified_agent_name}) claims")
        raise HTTPException(status_code=403, detail="Agent name mismatch")

    document_name_ru = verified_payload.get("document_name_ru")
    document_name_en = verified_payload.get("document_name_en") or ""
    justification_ru = verified_payload.get("justification_ru")
    justification_en = verified_payload.get("justification_en") or ""

    if not document_name_ru or not justification_ru:
        raise HTTPException(status_code=400, detail="Missing required claims (document_name_ru, justification_ru) in verified JWT")

    log_info("Agent Request", f"Verified request from {verified_agent_name} (kid: {active_key.kid}) for {document_name_ru}")
    return await crud.create_agent_request(
        db, verified_agent_name, document_name_ru, document_name_en, justification_ru, justification_en
    )

# --- МАРШРУТЫ: ПРОКСИ LLM ---
def extract_json_from_llm(result_text: str | None) -> dict | list:
    """
    Извлекает и парсит JSON из ответа LLM, даже если он содержит
    markdown-блоки (```json ... ```), теги рассуждений (<think>...) или поясняющий текст.
    """
    if not result_text or not result_text.strip():
        raise ValueError("Пустой ответ от LLM")
    
    # 1. Удаляем теги <think>...</think>, если они присутствуют
    cleaned = re.sub(r'<think>.*?</think>', '', result_text, flags=re.DOTALL).strip()
    
    # 2. Проверяем наличие markdown fenced block: ```json ... ``` или ``` ... ```
    fence_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', cleaned, re.IGNORECASE)
    if fence_match:
        content_candidate = fence_match.group(1).strip()
        try:
            return json.loads(content_candidate)
        except json.JSONDecodeError:
            pass

    # 3. Ищем самый внешний JSON-объект {...} или массив [...]
    start_brace = cleaned.find('{')
    start_bracket = cleaned.find('[')
    
    if start_brace != -1 and (start_bracket == -1 or start_brace < start_bracket):
        end_brace = cleaned.rfind('}')
        if end_brace != -1 and end_brace > start_brace:
            candidate = cleaned[start_brace:end_brace + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
    elif start_bracket != -1:
        end_bracket = cleaned.rfind(']')
        if end_bracket != -1 and end_bracket > start_bracket:
            candidate = cleaned[start_bracket:end_bracket + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

    # 4. В качестве fallback пробуем распарсить очищенную строку напрямую
    return json.loads(cleaned)

@app.post("/api/llm/analyze", response_model=schemas.LLMAnalyzeResponse)
async def llm_analyze(
    req: schemas.LLMAnalyzeRequest,
    current_admin: CurrentUser = Depends(require_permission(Permission.DOCUMENTS_WRITE))
):
    system_prompt = """
    ТЕБЕ НУЖНО ПРОАНАЛИЗИРОВАТЬ ТЕКСТ ДОКУМЕНТА И ИЗВЛЕЧЬ ДВА ПАРАМЕТРА: system_name И short_name.
    ИНСТРУКЦИЯ СТРОГАЯ, КАК ДЛЯ РЕБЕНКА (ОБЪЯСНЯЮ ПО ШАГАМ, ВЫПОЛНЯЙ В ТОЧНОСТИ):
    
    1. Что такое system_name:
       - Это уникальное системное имя документа. 
       - Оно должно содержать ТОЛЬКО латинские буквы (a-z), цифры (0-9) и символ подчеркивания (_). 
       - НИКАКИХ ПРОБЕЛОВ! НИКАКИХ СЛЕШЕЙ (/) ИЛИ ТОЧЕК (.)! НИКАКИХ РУССКИХ БУКВ!
       - Если видишь русские буквы - делай строгий транслит на английский.
       - Формат всегда такой: [тип_документа]_[номер_документа]_[дата_без_разделителей_ddmmyyyy]
       - Примеры как НАДО делать:
         - Если это "Федеральный Закон №115 от 01.01.2024", то system_name = "fz_115_01012024"
         - Если это "Письмо Банка России №123-И от 15.08.2023", то system_name = "pismo_br_123_i_15082023"
         - Если это "Приказ №1 от 05.02.2025", то system_name = "prikaz_1_05022025"
       
    2. Что такое short_name:
       - Это короткое имя документа (БЕЗ ДАТЫ на конце).
       - То есть ты берешь system_name и просто отрезаешь от него дату.
       - Примеры как НАДО делать:
         - Для "fz_115_01012024" -> short_name = "fz_115"
         - Для "pismo_br_123_i_15082023" -> short_name = "pismo_br_123_i"
    """
    
    prompt = system_prompt + "\n\nТЕКСТ ДЛЯ АНАЛИЗА:\n" + req.text[:15000]
    
    try:
        log_info("LLM", "Starting analyze request")
        response = await default_llm_client.acompletion(
            model=RAG_DATA_MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            response_format=schemas.LLMAnalyzeResponse,
            reasoning_effort=RAG_DATA_REASONING_EFFORT,
            temperature=0.0,
        )
        
        result_text = response.choices[0].message.content
        parsed = extract_json_from_llm(result_text)
        if not isinstance(parsed, dict):
            raise ValueError(f"Ожидался JSON-объект, получен {type(parsed)}")
        return schemas.LLMAnalyzeResponse(**parsed)
        
    except Exception as e:
        log_error("LLM", f"Error in analyze: {str(e)}")
        raise HTTPException(status_code=500, detail=f"LLM Error: {str(e)}")

@app.post("/api/llm/find_repealed", response_model=schemas.LLMRepealedResponse)
async def llm_find_repealed(
    req: schemas.LLMRepealedRequest,
    current_admin: CurrentUser = Depends(require_permission(Permission.DOCUMENTS_WRITE))
):
    system_prompt = """
    В тексте могут содержаться указания на отмену (утрату силы) старых нормативных актов.
    Твоя задача — найти все документы, которые ОТМЕНЯЮТСЯ (признаются утратившими силу) данным документом.
    Верни их в виде массива short_name (строгий транслит, только буквы, цифры, подчеркивания, без дат — точно так же, как мы формировали short_name ранее).
    
    ПРАВИЛА ИЗВЛЕЧЕНИЯ:
    1. Ищи фразы типа "Признать утратившим силу...", "Отменить..." и т.д.
    2. Извлекай тип документа и номер, превращай в транслит и соединяй через подчеркивание.
    3. Примеры:
       - Текст: "Признать утратившим силу Федеральный закон от 10.07.2002 № 86-ФЗ" -> short_name = "fz_86"
       - Текст: "Отменить Указание Банка России N 1234-У" -> short_name = "ukazanie_br_1234_u"
       - Текст: "Приказ Минфина России №10" -> short_name = "prikaz_minfina_10"
    4. Даты не включай в short_name! 
    5. Если ничего не отменяется, верни пустой массив [].
    """
    
    text_to_analyze = "\n\n---\n\n".join(req.snippets)
    prompt = system_prompt + "\n\nФРАГМЕНТЫ ДЛЯ АНАЛИЗА:\n" + text_to_analyze
    
    try:
        log_info("LLM", "Starting find_repealed request")
        response = await default_llm_client.acompletion(
            model=RAG_DATA_MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            response_format=schemas.LLMRepealedResponse,
            reasoning_effort=RAG_DATA_REASONING_EFFORT,
            temperature=0.0,
        )
        
        result_text = response.choices[0].message.content
        parsed = extract_json_from_llm(result_text)
        if isinstance(parsed, list):
            data = {"short_names": parsed}
        elif isinstance(parsed, dict):
            if "short_names" not in parsed and len(parsed) == 1:
                val = next(iter(parsed.values()))
                if isinstance(val, list):
                    data = {"short_names": val}
                else:
                    data = parsed
            else:
                data = parsed
        else:
            raise ValueError(f"Неожиданный формат данных от LLM: {type(parsed)}")
        return schemas.LLMRepealedResponse(**data)
        
    except Exception as e:
        log_error("LLM", f"Error in find_repealed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"LLM Error: {str(e)}")

# --- МАРШРУТЫ: УПРАВЛЕНИЕ ЗАЯВКАМИ АГЕНТОВ ---

@app.post("/api/agent-requests/approve", response_model=schemas.BatchActionResponse)
async def approve_agent_requests(
    req: schemas.AgentRequestBatchAction,
    db: AsyncSession = Depends(get_db),
    current_admin: CurrentUser = Depends(require_permission(Permission.REQUESTS_MANAGE))
):
    """Одобряет пакет заявок агентов по переданным идентификаторам."""
    await crud.update_agent_request_status(db, req.request_ids, "approved")
    await audit.log_audit_event(
        db,
        actor=current_admin,
        action="REQUESTS_APPROVED",
        status="SUCCESS",
        details={"request_ids": req.request_ids, "count": len(req.request_ids)}
    )
    return schemas.BatchActionResponse(status="ok")

@app.post("/api/agent-requests/reject", response_model=schemas.BatchActionResponse)
async def reject_agent_requests(
    req: schemas.AgentRequestBatchAction,
    db: AsyncSession = Depends(get_db),
    current_admin: CurrentUser = Depends(require_permission(Permission.REQUESTS_MANAGE))
):
    """Отклоняет пакет заявок агентов по переданным идентификаторам."""
    await crud.update_agent_request_status(db, req.request_ids, "rejected")
    await audit.log_audit_event(
        db,
        actor=current_admin,
        action="REQUESTS_REJECTED",
        status="SUCCESS",
        details={"request_ids": req.request_ids, "count": len(req.request_ids)}
    )
    return schemas.BatchActionResponse(status="ok")

@app.post("/api/agent-requests/bootstrap", response_model=schemas.AgentRequestResponse)
async def create_agent_request_endpoint(
    req: schemas.AgentRequestCreate,
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(...)
):
    """Создает заявку от имени агента с валидацией его bootstrap-токена."""
    expected_token = settings.get_bootstrap_token(req.agent_name)
    expected_auth = f"Bearer {expected_token}" if expected_token else ""
    if not expected_token or not secrets.compare_digest(authorization, expected_auth):
        log_warning("API", f"Invalid bootstrap token for agent {req.agent_name}")
        raise HTTPException(status_code=401, detail="Invalid token")
        
    created_req = await crud.create_agent_request(
        db, 
        req.agent_name, 
        req.document_name_ru, 
        req.document_name_en, 
        req.justification_ru, 
        req.justification_en
    )
    await audit.log_audit_event(
        db,
        actor=f"agent:{req.agent_name}",
        action="REQUEST_CREATED",
        status="SUCCESS",
        resource=f"request:{created_req.id}",
        details={"document_name_ru": req.document_name_ru, "agent": req.agent_name}
    )
    return created_req

# --- МАРШРУТЫ: ЗАЩИЩЕННЫЙ ЖУРНАЛ АУДИТА (TAMPER-EVIDENT AUDIT TRAIL) ---

@app.get("/api/audit/logs", response_model=schemas.AuditLogListResponse)
async def list_audit_logs(
    limit: int = Query(default=50, ge=1, le=200, description="Количество записей на страницу"),
    offset: int = Query(default=0, ge=0, description="Смещение пагинации"),
    actor: Optional[str] = Query(default=None, description="Фильтр по инициатору действия"),
    action: Optional[str] = Query(default=None, description="Фильтр по типу действия"),
    status: Optional[str] = Query(default=None, description="Фильтр по статусу (SUCCESS, FAILURE, WARNING)"),
    db: AsyncSession = Depends(get_db),
    current_admin: CurrentUser = Depends(require_permission(Permission.AUDIT_READ))
):
    """
    Возвращает постраничный список записей журнала аудита с хэш-цепочкой и возможностью фильтрации.
    Доступен только авторизованным администраторам.
    """
    items, total = await audit.get_audit_logs(
        db, limit=limit, offset=offset, actor=actor, action=action, status=status
    )
    return schemas.AuditLogListResponse(
        total=total,
        items=items,
        limit=limit,
        offset=offset
    )

@app.get("/api/audit/verify", response_model=schemas.AuditVerifyResponse)
async def verify_audit_trail(
    db: AsyncSession = Depends(get_db),
    current_admin: CurrentUser = Depends(require_permission(Permission.AUDIT_VERIFY))
):
    """
    Запускает криптографическую верификацию всей цепочки записей журнала аудита (Tamper Detection).
    Проверяет целостность каждого SHA-256 хэша и связь prev_hash -> record_hash.
    """
    result = await audit.verify_audit_chain(db)
    return schemas.AuditVerifyResponse(**result)

@app.get("/api/audit/summary", response_model=schemas.AuditSummaryResponse)
async def get_audit_trail_summary(
    db: AsyncSession = Depends(get_db),
    current_admin: CurrentUser = Depends(require_permission(Permission.AUDIT_READ))
):
    """
    Возвращает статистику журнала аудита и актуальный статус криптографической целостности цепочки.
    """
    summary = await audit.get_audit_summary(db)
    return schemas.AuditSummaryResponse(**summary)
