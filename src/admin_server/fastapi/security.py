import os
import re
import uuid
import secrets
import hmac
import hashlib
import struct
import time
import base64
from typing import Any, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
import jwt
import bcrypt
try:
    import pyotp
except ImportError:
    pyotp = None
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
try:
    import redis  # type: ignore
except ImportError:
    redis = None
try:
    from minio import sse as minio_sse  # type: ignore
except ImportError:
    minio_sse = None
from core.utils.console_logger import log_error, log_warning, log_info

try:
    from rbac import (
        Role,
        Permission,
        ROLE_PERMISSIONS,
        ROLE_DESCRIPTIONS,
        CurrentUser,
        get_permissions_for_role,
    )
except ImportError:
    from .rbac import (
        Role,
        Permission,
        ROLE_PERMISSIONS,
        ROLE_DESCRIPTIONS,
        CurrentUser,
        get_permissions_for_role,
    )

VALID_ADMIN_USERS: set[str] = {"admin", "operator", "auditor"}

class Settings:
    """Конфигурация параметров безопасности и аутентификации на основе асимметричного алгоритма RS256, 2FA и шифрования данных при хранении."""
    ALGORITHM: str = "RS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "15"))  # 15 минут вместо 12 часов
    REFRESH_TOKEN_EXPIRE_DAYS: int = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7"))      # 7 дней для refresh-токена
    ADMIN_PASSWORD_HASH: str = os.getenv("ADMIN_PASSWORD_HASH", "")
    ADMIN_TOTP_SECRET: str = os.getenv("ADMIN_TOTP_SECRET", "").strip()
    REQUIRE_2FA: bool = os.getenv("REQUIRE_2FA", "false").lower() in ("true", "1", "yes")
    
    # Параметры шифрования при хранении (Data at Rest / AES-256-GCM / MinIO SSE)
    DATA_ENCRYPTION_KEY: str = os.getenv("DATA_ENCRYPTION_KEY", "").strip()
    MINIO_SSE_ENABLED: bool = os.getenv("MINIO_SSE_ENABLED", "false").lower() in ("true", "1", "yes")
    MINIO_SSE_TYPE: str = os.getenv("MINIO_SSE_TYPE", "s3").lower().strip()
    MINIO_SSE_C_KEY: str = os.getenv("MINIO_SSE_C_KEY", "").strip()
    MINIO_KMS_SECRET_KEY: str = os.getenv("MINIO_KMS_SECRET_KEY", "").strip()

    # Параметры взаимного TLS (mTLS) и защиты сетевого трафика (In-Transit Encryption)
    REQUIRE_MTLS: bool = os.getenv("REQUIRE_MTLS", "false").lower() in ("true", "1", "yes")
    ALLOWED_MTLS_SUBJECTS: list[str] = [
        s.strip() for s in os.getenv("ALLOWED_MTLS_SUBJECTS", "").split(",") if s.strip()
    ]
    ALLOWED_MTLS_ISSUERS: list[str] = [
        s.strip() for s in os.getenv("ALLOWED_MTLS_ISSUERS", "").split(",") if s.strip()
    ]
    MINIO_SECURE: bool = os.getenv("MINIO_SECURE", "false").lower() in ("true", "1", "yes")
    REDIS_SSL: bool = os.getenv("REDIS_SSL", "false").lower() in ("true", "1", "yes")
    POSTGRES_SSLMODE: str = os.getenv("POSTGRES_SSLMODE", "prefer").strip()

    AGENT_DIGITAL_BOOTSTRAP_TOKEN: str = os.getenv("AGENT_DIGITAL_BOOTSTRAP_TOKEN", "")
    AGENT_BANK_BOOTSTRAP_TOKEN: str = os.getenv("AGENT_BANK_BOOTSTRAP_TOKEN", "")
    AGENT_INVEST_BOOTSTRAP_TOKEN: str = os.getenv("AGENT_INVEST_BOOTSTRAP_TOKEN", "")
    AGENT_MAIN_BOOTSTRAP_TOKEN: str = os.getenv("AGENT_MAIN_BOOTSTRAP_TOKEN", "")

    PRIVATE_KEY: str = ""
    PUBLIC_KEY: str = ""

    def __init__(self):
        self.load_keys()

    def load_keys(self) -> None:
        """
        Загружает или генерирует ключевую пару RSA (RS256).
        Приоритет:
        1. Файлы по путям JWT_PRIVATE_KEY_PATH / JWT_PUBLIC_KEY_PATH
        2. Строки PEM в переменных окружения JWT_PRIVATE_KEY / JWT_PUBLIC_KEY
        3. Автоматическая генерация эфемерной 2048-битной RSA пары (dev / test режим)
        """
        priv_path = os.getenv("JWT_PRIVATE_KEY_PATH", "").strip()
        pub_path = os.getenv("JWT_PUBLIC_KEY_PATH", "").strip()
        priv_raw = os.getenv("JWT_PRIVATE_KEY", "").strip()
        pub_raw = os.getenv("JWT_PUBLIC_KEY", "").strip()

        priv_pem = ""
        pub_pem = ""

        # 1. Приватный ключ из файла
        if priv_path and os.path.exists(priv_path):
            try:
                with open(priv_path, "r", encoding="utf-8") as f:
                    priv_pem = f.read().strip()
            except Exception as e:
                log_error("Безопасность", f"Не удалось прочитать приватный ключ из файла {priv_path}: {e}")
        elif priv_raw:
            priv_pem = priv_raw.replace("\\n", "\n").strip()

        # 2. Публичный ключ из файла
        if pub_path and os.path.exists(pub_path):
            try:
                with open(pub_path, "r", encoding="utf-8") as f:
                    pub_pem = f.read().strip()
            except Exception as e:
                log_error("Безопасность", f"Не удалось прочитать публичный ключ из файла {pub_path}: {e}")
        elif pub_raw:
            pub_pem = pub_raw.replace("\\n", "\n").strip()

        # 3. Деривация публичного ключа из приватного, если публичный не задан явно
        if priv_pem and not pub_pem:
            try:
                loaded_priv = serialization.load_pem_private_key(priv_pem.encode("utf-8"), password=None)
                pub_pem = loaded_priv.public_key().public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo
                ).decode("utf-8")
            except Exception as e:
                log_error("Безопасность", f"Не удалось извлечь публичный ключ из приватного ключа: {e}")

        # 4. Если ключи не настроены (dev/test режим) - генерация надежной RSA-2048 пары
        if not priv_pem or not pub_pem:
            is_strict = os.getenv("STRICT_SECURITY", "false").lower() in ("true", "1") or os.getenv("ENV") == "production"
            if is_strict:
                raise RuntimeError(
                    "Критические ключи подписи JWT (JWT_PRIVATE_KEY_PATH / JWT_PRIVATE_KEY) не настроены. "
                    "В production/strict режиме генерация эфемерных ключей запрещена."
                )
            log_warning(
                "Безопасность",
                "RSA-ключи подписи JWT не настроены. Сгенерирована временная пара ключей в памяти (RS256, 2048-bit). "
                "Для промышленного контура обязательно настройте JWT_PRIVATE_KEY_PATH / JWT_PUBLIC_KEY_PATH."
            )
            generated_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            priv_pem = generated_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption()
            ).decode("utf-8")
            pub_pem = generated_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo
            ).decode("utf-8")

        self.PRIVATE_KEY = priv_pem
        self.PUBLIC_KEY = pub_pem

    @property
    def SECRET_KEY(self) -> str:
        """Свойство обратной совместимости с функциями декодирования: возвращает публичный ключ."""
        return self.PUBLIC_KEY

    def is_2fa_enabled(self) -> bool:
        """Проверяет, включена ли обязательная двухфакторная аутентификация 2FA."""
        return bool(self.ADMIN_TOTP_SECRET or self.REQUIRE_2FA)

    def is_minio_sse_enabled(self) -> bool:
        """Проверяет, включено ли серверное шифрование MinIO SSE."""
        if self.MINIO_SSE_ENABLED:
            return True
        return bool(self.MINIO_KMS_SECRET_KEY or self.MINIO_SSE_C_KEY)

    def validate_production_env(self) -> None:
        """Проверяет наличие обязательных переменных окружения для безопасной работы в продакшене."""
        missing = []
        has_priv = bool(os.getenv("JWT_PRIVATE_KEY") or os.getenv("JWT_PRIVATE_KEY_PATH"))
        if not has_priv:
            missing.append("JWT_PRIVATE_KEY или JWT_PRIVATE_KEY_PATH")
        for agent in ["DIGITAL", "BANK", "INVEST", "MAIN"]:
            if not getattr(self, f"AGENT_{agent}_BOOTSTRAP_TOKEN"):
                missing.append(f"AGENT_{agent}_BOOTSTRAP_TOKEN")
        data_key = self.DATA_ENCRYPTION_KEY or os.getenv("DATA_ENCRYPTION_KEY", "")
        if not data_key:
            missing.append("DATA_ENCRYPTION_KEY (обязательно для шифрования данных при хранении / Data at Rest)")
        if missing:
            raise RuntimeError(f"Отсутствуют обязательные переменные окружения безопасности: {', '.join(missing)}")
        if not self.ADMIN_TOTP_SECRET:
            log_info(
                "Безопасность",
                "ADMIN_TOTP_SECRET не задан в .env. Сервер ожидает загрузку секрета из зашифрованной базы данных или первичную привязку 2FA (onboarding)."
            )

    def get_bootstrap_token(self, agent_name: str) -> str | None:
        """Возвращает bootstrap-токен для указанного агента."""
        tokens = {
            "digital": self.AGENT_DIGITAL_BOOTSTRAP_TOKEN,
            "bank": self.AGENT_BANK_BOOTSTRAP_TOKEN,
            "invest": self.AGENT_INVEST_BOOTSTRAP_TOKEN,
            "main": self.AGENT_MAIN_BOOTSTRAP_TOKEN,
        }
        return tokens.get(agent_name)


settings = Settings()

# Настройка Redis для JWT Blacklist
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")
REDIS_SSL_CA = os.getenv("REDIS_SSL_CA_CERTS", "/certs/ca.crt")

try:
    if redis is not None:
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
        redis_blacklist = redis.Redis(**redis_kwargs)
    else:
        redis_blacklist = None
except Exception as e:
    log_error("Аутентификация", f"Не удалось инициализировать клиент Redis для черного списка: {e}")
    redis_blacklist = None

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Проверяет соответствие открытого пароля его bcrypt-хэшу."""
    try:
        return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))
    except Exception:
        return False

def get_password_hash(password: str) -> str:
    """Формирует bcrypt-хэш из переданного пароля."""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def create_access_token(data: dict, role: str | Role | None = None) -> str:
    """Создает подписанный асимметричным приватным ключом RSA (RS256) короткоживущий (15 мин) access-токен с уникальным jti и правами RBAC."""
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    
    raw_role = role or to_encode.get("role") or Role.SUPERADMIN
    if isinstance(raw_role, Role):
        role_str = raw_role.value
    else:
        role_str = str(raw_role).lower().strip()

    try:
        role_enum = Role(role_str)
    except ValueError:
        role_enum = Role.SUPERADMIN
        role_str = Role.SUPERADMIN.value

    perms = [p.value for p in ROLE_PERMISSIONS.get(role_enum, set())]
    sub = to_encode.get("sub", "admin")

    to_encode.update({
        "sub": sub,
        "role": role_str,
        "permissions": perms,
        "type": "access",
        "iat": now,
        "exp": expire,
        "jti": str(uuid.uuid4())
    })
    encoded_jwt = jwt.encode(to_encode, settings.PRIVATE_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt

def create_refresh_token(data: dict, role: str | Role | None = None) -> str:
    """Создает подписанный асимметричным приватным ключом RSA (RS256) refresh-токен (7 дней) для ротации (RTR) с сохранением роли."""
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    expire = now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)

    raw_role = role or to_encode.get("role") or Role.SUPERADMIN
    if isinstance(raw_role, Role):
        role_str = raw_role.value
    else:
        role_str = str(raw_role).lower().strip()

    try:
        role_enum = Role(role_str)
    except ValueError:
        role_enum = Role.SUPERADMIN
        role_str = Role.SUPERADMIN.value

    sub = to_encode.get("sub", "admin")
    to_encode.update({
        "sub": sub,
        "role": role_str,
        "type": "refresh",
        "iat": now,
        "exp": expire,
        "jti": str(uuid.uuid4())
    })
    encoded_jwt = jwt.encode(to_encode, settings.PRIVATE_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt

def decode_access_token(token: str) -> dict:
    """Декодирует и валидирует токен публичным RSA-ключом (RS256), защищая от подделки и подмены алгоритма."""
    return jwt.decode(token, settings.PUBLIC_KEY, algorithms=[settings.ALGORITHM])

async def get_current_admin(token: str = Depends(oauth2_scheme)) -> CurrentUser:
    """
    Проверяет JWT access-токен администратора по RS256, валидирует его статус в черном списке Redis
    и возвращает CurrentUser с ролью и гранулярными разрешениями.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token)
        username: str = payload.get("sub")
        token_type: str = payload.get("type", "access")
        if not username or token_type != "access":
            raise credentials_exception
        if username not in VALID_ADMIN_USERS:
            raise credentials_exception
    except jwt.PyJWTError:
        raise credentials_exception

    # Проверяем отзыв токена в Redis по jti или токену целиком
    jti = payload.get("jti")
    is_revoked = False
    if redis_blacklist:
        try:
            if (jti and redis_blacklist.exists(f"blacklist:{jti}")) or redis_blacklist.exists(f"blacklist:{token}"):
                is_revoked = True
        except Exception as e:
            log_error("Аутентификация", f"Предупреждение безопасности: сбой проверки черного списка в Redis: {e}")
            if os.getenv("AUTH_FAIL_CLOSED", "false").lower() == "true":
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Authentication service temporarily unavailable",
                    headers={"WWW-Authenticate": "Bearer"},
                )
    else:
        log_warning("Аутентификация", "Черный список Redis не инициализирован; проверка отзыва токена невозможна")
            
    if is_revoked:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Извлекаем роль и разрешения
    raw_role = payload.get("role", "superadmin")
    try:
        role_enum = Role(str(raw_role).lower().strip())
    except ValueError:
        role_enum = Role.SUPERADMIN

    raw_perms = payload.get("permissions")
    if raw_perms and isinstance(raw_perms, list):
        perms = set()
        for p in raw_perms:
            try:
                perms.add(Permission(p))
            except ValueError:
                pass
    else:
        perms = ROLE_PERMISSIONS.get(role_enum, set()).copy()

    return CurrentUser(username=username, role=role_enum, permissions=perms)

def verify_and_rotate_refresh_token(refresh_token: str) -> tuple[str, str]:
    """
    Проверяет refresh-токен, инвалидирует его в черном списке Redis (Refresh Token Rotation)
    и выпускает новую пару (access_token, refresh_token).
    Защищает от повторного использования токенов обновления (Reuse Detection).
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired refresh token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(refresh_token)
        username = payload.get("sub")
        token_type = payload.get("type")
        jti = payload.get("jti")
        if not username or username not in VALID_ADMIN_USERS or token_type != "refresh" or not jti:
            raise credentials_exception
    except jwt.PyJWTError:
        raise credentials_exception

    # Обнаружение повторного использования (Reuse Detection)
    if redis_blacklist:
        try:
            if redis_blacklist.exists(f"blacklist:{jti}") or redis_blacklist.exists(f"blacklist:{refresh_token}"):
                log_error("Безопасность", f"Попытка повторного использования отозванного refresh-токена (jti={jti})! Возможная компрометация сессии.")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Refresh token already used or revoked",
                    headers={"WWW-Authenticate": "Bearer"},
                )
        except HTTPException:
            raise
        except Exception as e:
            log_error("Безопасность", f"Сбой проверки черного списка Redis при ротации refresh-токена: {e}")
            if os.getenv("AUTH_FAIL_CLOSED", "false").lower() == "true":
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Authentication service temporarily unavailable",
                )

        # Инвалидируем использованный refresh-токен
        try:
            now_ts = int(datetime.now(timezone.utc).timestamp())
            exp = payload.get("exp")
            ttl = max(1, exp - now_ts) if exp else settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400
            redis_blacklist.setex(f"blacklist:{jti}", ttl, "rotated")
            redis_blacklist.setex(f"blacklist:{refresh_token}", ttl, "rotated")
        except Exception as e:
            log_error("Безопасность", f"Сбой инвалидации старого refresh-токена в Redis: {e}")

    role_str = payload.get("role", "superadmin")
    new_access = create_access_token(data={"sub": username}, role=role_str)
    new_refresh = create_refresh_token(data={"sub": username}, role=role_str)
    return new_access, new_refresh


async def get_current_user(current_admin: Any = Depends(get_current_admin)) -> CurrentUser:
    """
    Зависимость FastAPI для извлечения текущего пользователя.
    Поддерживает прямые вызовы get_current_admin, а также тестовые overrides.
    """
    if isinstance(current_admin, CurrentUser):
        return current_admin
    return CurrentUser(username=str(current_admin), role=Role.SUPERADMIN)


def require_role(*required_roles: Role | str) -> Callable:
    """
    Фабрика зависимостей: проверяет, что роль текущего пользователя входит в список разрешенных.
    """
    valid_role_values = {
        r.value if isinstance(r, Role) else str(r).lower().strip()
        for r in required_roles
    }

    async def role_checker(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role.value not in valid_role_values:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: insufficient role privileges. Required one of: {sorted(list(valid_role_values))}"
            )
        return user

    return role_checker


def require_permission(*required_permissions: Permission | str) -> Callable:
    """
    Фабрика зависимостей: проверяет, что текущий пользователь обладает всеми требуемыми разрешениями.
    """
    perm_enums: list[Permission] = []
    for p in required_permissions:
        if isinstance(p, Permission):
            perm_enums.append(p)
        else:
            try:
                perm_enums.append(Permission(p))
            except ValueError:
                pass

    async def permission_checker(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        for perm in perm_enums:
            if not user.has_permission(perm):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Forbidden: missing required permission '{perm.value}'"
                )
        return user

    return permission_checker


def calculate_key_fingerprint(public_key_pem: str) -> str:
    """Вычисляет SHA-256 отпечаток публичного ключа для идентификации и аудита."""
    try:
        clean_pem = public_key_pem.strip()
        digest = hashlib.sha256(clean_pem.encode("utf-8")).hexdigest()
        return f"sha256:{digest[:16]}"
    except Exception:
        return "sha256:unknown"

def generate_key_id(agent_name: str, public_key_pem: str) -> str:
    """Генерирует уникальный идентификатор ключа (Key ID / kid) на основе имени и хэша ключа."""
    digest = hashlib.sha256(public_key_pem.strip().encode("utf-8")).hexdigest()[:12]
    return f"{agent_name}-{digest}"

def revoke_agent_key_in_redis(agent_name: str, kid: str | None = None, ttl_seconds: int = 86400 * 90):
    """Помещает отозванный ключ агента в черный список Redis для мгновенного отклонения запросов."""
    if redis_blacklist:
        try:
            if kid:
                redis_blacklist.setex(f"revoked:agent_key:{kid}", ttl_seconds, "revoked")
            redis_blacklist.setex(f"revoked:agent:{agent_name}", ttl_seconds, "revoked")
        except Exception as e:
            log_error("Безопасность", f"Ошибка сохранения отзыва ключа агента в Redis: {e}")

def is_agent_key_revoked_in_redis(agent_name: str, kid: str | None = None) -> bool:
    """Проверяет статус отзыва ключа агента в черном списке Redis."""
    if redis_blacklist:
        try:
            if kid and redis_blacklist.exists(f"revoked:agent_key:{kid}"):
                return True
            if redis_blacklist.exists(f"revoked:agent:{agent_name}"):
                return True
        except Exception as e:
            log_warning("Безопасность", f"Ошибка проверки отзыва агента в Redis: {e}")
    return False

def verify_agent_jwt(token: str, public_key: str, expected_kid: str | None = None) -> dict | None:
    """Проверяет подпись JWT-токена агента по алгоритму RS256 с валидацией kid."""
    try:
        if expected_kid:
            unverified_headers = jwt.get_unverified_header(token)
            token_kid = unverified_headers.get("kid")
            if token_kid and token_kid != expected_kid:
                log_warning("Аутентификация", f"Несовпадение Key ID: в токене {token_kid}, ожидался {expected_kid}")
                return None
        payload = jwt.decode(token, public_key, algorithms=["RS256"])
        return payload
    except Exception as e:
        log_warning("Аутентификация", f"Сбой проверки JWT-токена агента: {e}")
        return None

def generate_totp_secret() -> str:
    """Генерирует криптографически стойкий 160-битный base32-секрет для TOTP (RFC 6238)."""
    try:
        if pyotp is not None:
            return pyotp.random_base32()
    except Exception:
        pass
    raw_bytes = secrets.token_bytes(20)
    return base64.b32encode(raw_bytes).decode("utf-8").rstrip("=")

def get_totp_uri(secret: str, username: str = "admin", issuer: str = "Financial-AI-Agent") -> str:
    """Формирует URI otpauth://totp/... для сканирования QR-кода в Google Authenticator / FreeOTP."""
    try:
        if pyotp is not None:
            return pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)
    except Exception:
        pass
    import urllib.parse
    encoded_issuer = urllib.parse.quote(issuer)
    encoded_username = urllib.parse.quote(username)
    return f"otpauth://totp/{encoded_issuer}:{encoded_username}?secret={secret}&issuer={encoded_issuer}&algorithm=SHA1&digits=6&period=30"

def verify_totp_code(secret: str, code: str, window: int = 1) -> bool:
    """
    Проверяет 6-значный одноразовый TOTP код (RFC 6238) с окном дрейфа времени ±30 сек.
    Поддерживает pyotp, а также стандартный алгоритм RFC 6238.
    """
    if not secret or not code:
        return False
    code = code.strip()
    if len(code) != 6 or not code.isdigit():
        return False

    # 1. Попытка через pyotp
    try:
        if pyotp is not None:
            totp = pyotp.TOTP(secret)
            if totp.verify(code, valid_window=window):
                return True
    except Exception:
        pass

    # 2. Стандартный алгоритм RFC 6238 (HMAC-SHA1)
    try:
        now = time.time()
        time_step = 30
        digits = 6
        padding = "=" * ((8 - len(secret) % 8) % 8)
        key = base64.b32decode((secret + padding).upper(), casefold=True)
        current_counter = int(now // time_step)
        for drift in range(-window, window + 1):
            counter = current_counter + drift
            msg = struct.pack(">Q", counter)
            h = hmac.new(key, msg, hashlib.sha1).digest()
            offset = h[19] & 0x0F
            token_val = (struct.unpack(">I", h[offset:offset+4])[0] & 0x7FFFFFFF) % (10 ** digits)
            expected_code = f"{token_val:0{digits}d}"
            if hmac.compare_digest(expected_code, code):
                return True
    except Exception:
        return False

    return False

def generate_qr_svg(uri: str) -> str:
    """Генерирует SVG-изображение QR-кода для безопасного отображения в интерфейсе без сторонних CDN."""
    try:
        import qrcode
        import qrcode.image.svg
        import io
        factory = qrcode.image.svg.SvgPathImage
        img = qrcode.make(uri, image_factory=factory)
        stream = io.BytesIO()
        img.save(stream)
        return stream.getvalue().decode("utf-8")
    except Exception as e:
        log_warning("2FA", f"Не удалось сгенерировать QR SVG: {e}")
        return ""


# Временное состояние для первичной настройки 2FA (onboarding)
_pending_totp_cache: dict[str, tuple[str, float]] = {}


def set_pending_totp_secret(username: str, secret: str, ttl_seconds: int = 300, redis_conn=None) -> None:
    """Сохраняет временный TOTP-секрет для первичной привязки в Redis и локальный кэш."""
    client = redis_conn or redis_blacklist
    if client:
        try:
            client.setex(f"auth:2fa_pending:{username}", ttl_seconds, secret)
        except Exception as e:
            log_warning("2FA", f"Не удалось сохранить временный TOTP-секрет в Redis: {e}")
    _pending_totp_cache[username] = (secret, time.time() + ttl_seconds)


def get_pending_totp_secret(username: str, redis_conn=None) -> str | None:
    """Извлекает временный TOTP-секрет из Redis или локального кэша, если срок действия не истек."""
    client = redis_conn or redis_blacklist
    if client:
        try:
            val = client.get(f"auth:2fa_pending:{username}")
            if val is not None:
                if isinstance(val, bytes):
                    return val.decode("utf-8")
                return str(val)
        except Exception as e:
            log_warning("2FA", f"Не удалось получить временный TOTP-секрет из Redis: {e}")

    cached = _pending_totp_cache.get(username)
    if cached:
        secret, expires_at = cached
        if time.time() <= expires_at:
            return secret
        else:
            _pending_totp_cache.pop(username, None)
    return None


def clear_pending_totp_secret(username: str, redis_conn=None) -> None:
    """Удаляет временный TOTP-секрет после успешной валидации или истечения времени."""
    client = redis_conn or redis_blacklist
    if client:
        try:
            client.delete(f"auth:2fa_pending:{username}")
        except Exception:
            pass
    _pending_totp_cache.pop(username, None)


# --- ШИФРОВАНИЕ ДАННЫХ ПРИ ХРАНЕНИИ (DATA AT REST / AES-256-GCM / MINIO SSE) ---


MAGIC_ENCRYPTION_V1 = b"ENC1"
_EPHEMERAL_DATA_KEY: bytes | None = None


def resolve_encryption_key(key: bytes | str | None = None) -> bytes:
    """
    Разрешает и валидирует 256-битный (32-байтный) ключ шифрования AES-GCM.
    Принимает:
      - bytes (ровно 32 байта)
      - hex-строку (64 символа)
      - base64-строку (32 декодированных байта)
      - произвольную строку (деривация через SHA-256)
      - None (берется из settings.DATA_ENCRYPTION_KEY или генерируется эфемерный ключ)
    """
    global _EPHEMERAL_DATA_KEY
    if key is None:
        raw_key = settings.DATA_ENCRYPTION_KEY or os.getenv("DATA_ENCRYPTION_KEY", "")
        if not raw_key:
            is_strict = os.getenv("STRICT_SECURITY", "false").lower() in ("true", "1") or os.getenv("ENV") == "production"
            if is_strict:
                raise RuntimeError(
                    "DATA_ENCRYPTION_KEY не настроен. "
                    "В production/strict режиме генерация эфемерных ключей шифрования запрещена."
                )
            if _EPHEMERAL_DATA_KEY is None:
                _EPHEMERAL_DATA_KEY = os.urandom(32)
                log_warning(
                    "Шифрование",
                    "DATA_ENCRYPTION_KEY не настроен в переменных окружения. "
                    "Сгенерирован временный 256-битный ключ в памяти. "
                    "Для промышленного контура обязательно настройте DATA_ENCRYPTION_KEY."
                )
            return _EPHEMERAL_DATA_KEY
        key = raw_key

    if isinstance(key, (bytes, bytearray)):
        if len(key) == 32:
            return bytes(key)
        raise ValueError(f"Ключ шифрования (bytes) должен быть ровно 32 байта (256 бит), получено {len(key)}")

    if isinstance(key, str):
        key = key.strip()
        if not key:
            raise ValueError("Ключ шифрования не может быть пустой строкой")
        # 1. Попытка распарсить как 64-символьный hex
        if len(key) == 64:
            try:
                candidate = bytes.fromhex(key)
                if len(candidate) == 32:
                    return candidate
            except ValueError:
                pass
        # 2. Попытка распарсить как Base64
        try:
            candidate = base64.b64decode(key)
            if len(candidate) == 32:
                return candidate
        except Exception:
            pass
        # 3. Если длина ровно 32 символа в ASCII
        encoded = key.encode("utf-8")
        if len(encoded) == 32:
            return encoded
        # 4. Деривация через SHA-256 для произвольной кодовой фразы
        return hashlib.sha256(encoded).digest()

    raise TypeError(f"Неподдерживаемый тип ключа шифрования: {type(key).__name__}")


def encrypt_data_at_rest(
    data: bytes | str,
    key: bytes | str | None = None,
    associated_data: bytes | None = None
) -> bytes:
    """
    Шифрует данные для безопасного хранения в БД/томах с использованием AES-256-GCM.
    
    Формат зашифрованного пакета (Envelope):
    [4 байта: MAGIC 'ENC1'] + [12 байтов: случайный Nonce/IV] + [Зашифрованные данные + 16 байт Auth Tag]
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    elif not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"Ожидались bytes или str, получено: {type(data).__name__}")

    resolved_key = resolve_encryption_key(key)
    nonce = os.urandom(12)
    aesgcm = AESGCM(resolved_key)
    ciphertext_and_tag = aesgcm.encrypt(nonce, bytes(data), associated_data)
    return MAGIC_ENCRYPTION_V1 + nonce + ciphertext_and_tag


def decrypt_data_at_rest(
    encrypted_payload: bytes,
    key: bytes | str | None = None,
    associated_data: bytes | None = None
) -> bytes:
    """
    Расшифровывает данные с проверкой аутентичности и целостности (AEAD AES-256-GCM).
    Бросает ValueError при несовпадении тега, повреждении данных или неверном ключе.
    """
    if not isinstance(encrypted_payload, (bytes, bytearray)):
        raise TypeError(f"Ожидались bytes, получено: {type(encrypted_payload).__name__}")

    min_len = len(MAGIC_ENCRYPTION_V1) + 12 + 16  # Magic (4) + Nonce (12) + Tag (16)
    if len(encrypted_payload) < min_len:
        raise ValueError("Зашифрованные данные повреждены: длина пакета меньше минимально допустимой.")

    if not encrypted_payload.startswith(MAGIC_ENCRYPTION_V1):
        raise ValueError("Неподдерживаемый формат шифрования или отсутствующий заголовок ENC1.")

    header_len = len(MAGIC_ENCRYPTION_V1)
    nonce = encrypted_payload[header_len : header_len + 12]
    ciphertext_and_tag = encrypted_payload[header_len + 12 :]

    resolved_key = resolve_encryption_key(key)
    aesgcm = AESGCM(resolved_key)

    try:
        return aesgcm.decrypt(nonce, ciphertext_and_tag, associated_data)
    except Exception as e:
        raise ValueError("Ошибка расшифровки данных: неверный ключ, поврежденный шифротекст или несовпадение тега аутентификации.") from e


def encrypt_text_at_rest(
    text: str,
    key: bytes | str | None = None,
    associated_data: bytes | None = None
) -> str:
    """Шифрует строковые данные и возвращает безопасную Base64url-строку."""
    raw_cipher = encrypt_data_at_rest(text.encode("utf-8"), key=key, associated_data=associated_data)
    return base64.urlsafe_b64encode(raw_cipher).decode("ascii")


def decrypt_text_at_rest(
    encrypted_b64: str,
    key: bytes | str | None = None,
    associated_data: bytes | None = None
) -> str:
    """Расшифровывает Base64url-строку и возвращает исходный UTF-8 текст."""
    if not isinstance(encrypted_b64, str):
        raise TypeError(f"Ожидалась строка Base64, получено: {type(encrypted_b64).__name__}")
    try:
        raw_cipher = base64.urlsafe_b64decode(encrypted_b64.encode("ascii"))
    except Exception as e:
        raise ValueError("Некорректная Base64 строка зашифрованных данных.") from e
    decrypted_bytes = decrypt_data_at_rest(raw_cipher, key=key, associated_data=associated_data)
    return decrypted_bytes.decode("utf-8")


def get_minio_sse(
    sse_type: str | None = None,
    customer_key: bytes | str | None = None
):
    """
    Возвращает объект ServerSideEncryption (SSE-S3 или SSE-C) для MinIO client.
    Обеспечивает шифрование объектов в объектном хранилище MinIO при хранении (Data at Rest).
    """
    if sse_type is None and not settings.is_minio_sse_enabled():
        return None

    target_type = (sse_type or settings.MINIO_SSE_TYPE or "s3").lower().strip()
    if target_type in ("none", "disabled", "false"):
        return None

    sse_mod = None
    try:
        import minio.sse as actual_sse
        sse_mod = actual_sse
    except Exception:
        sse_mod = minio_sse

    if sse_mod is None or not hasattr(sse_mod, "SseS3"):
        log_warning("MinIO SSE", "Модуль minio.sse недоступен, Server-Side Encryption отключено.")
        return None

    if target_type == "ssec":
        key_source = customer_key or settings.MINIO_SSE_C_KEY or settings.DATA_ENCRYPTION_KEY
        if not key_source:
            log_warning("MinIO SSE", "Ключ SSE-C не настроен, переключение на стандартный SSE-S3.")
            return sse_mod.SseS3()
        key_32 = resolve_encryption_key(key_source)
        return sse_mod.SseCustomerKey(key_32)

    # По умолчанию SSE-S3 (AES-256)
    return sse_mod.SseS3()


# --- ХЕЛПЕРЫ КОНВЕРТНОГО ШИФРОВАНИЯ CHROMADB (ENVELOPE ENCRYPTION) ---
# Каноническая реализация находится в src/admin_server/vectors/chroma_envelope.py.
# Реэкспортируем функции для сохранения обратной совместимости интерфейса security.
try:
    from vectors.chroma_envelope import (
        is_encrypted_chunk,
        encrypt_chroma_chunk,
        decrypt_chroma_chunk,
        encrypt_chroma_metadata,
        decrypt_chroma_metadata,
        decrypt_chroma_results,
    )
except ImportError:
    try:
        from src.admin_server.vectors.chroma_envelope import (
            is_encrypted_chunk,
            encrypt_chroma_chunk,
            decrypt_chroma_chunk,
            encrypt_chroma_metadata,
            decrypt_chroma_metadata,
            decrypt_chroma_results,
        )
    except ImportError:
        pass


def extract_cn_from_subject(subject: str | None) -> str | None:
    """Извлекает Common Name (CN) из строки Subject сертификата (например, 'CN=admin-client,O=FinancialAI')."""
    if not subject:
        return None
    match = re.search(r'(?:^|[\/,\s])CN\s*=\s*([^,\/]+)', subject, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def parse_mtls_client_certificate(request: Request) -> dict[str, Any]:
    """
    Извлекает и нормализует метаданные mTLS клиентского сертификата из HTTP-заголовков reverse proxy (Caddy/Nginx).
    """
    verify_header = request.headers.get("x-client-cert-verify", "").strip()
    # Caddy передает "SUCCESS" или статус верификации
    is_verified = verify_header.upper() in ("SUCCESS", "TRUE", "1")

    subject = request.headers.get("x-client-cert-subject", "").strip() or None
    issuer = request.headers.get("x-client-cert-issuer", "").strip() or None
    serial = request.headers.get("x-client-cert-serial", "").strip() or None
    fingerprint = request.headers.get("x-client-cert-fingerprint", "").strip() or None

    common_name = extract_cn_from_subject(subject) if subject else None

    return {
        "verified": is_verified,
        "raw_verify_status": verify_header or "NONE",
        "subject": subject,
        "issuer": issuer,
        "serial": serial,
        "fingerprint": fingerprint,
        "common_name": common_name,
        "is_present": bool(verify_header and verify_header.upper() != "NONE"),
    }


def verify_mtls_client_certificate(request: Request) -> dict[str, Any]:
    """
    Зависимость FastAPI для проверки клиентского mTLS-сертификата.
    Если REQUIRE_MTLS=true, проверяет успешность валидации сертификата и соответствие списку разрешенных субъектов/эмитентов.
    """
    cert_info = parse_mtls_client_certificate(request)

    if not settings.REQUIRE_MTLS:
        return cert_info

    if not cert_info["verified"]:
        log_warning("mTLS", f"Отказ в доступе: клиент не предоставил валидный TLS-сертификат (статус: {cert_info['raw_verify_status']})")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется действительный клиентский TLS-сертификат (mTLS Mutual Authentication Required)"
        )

    # Проверка разрешенных субъектов (Common Name или полный Subject)
    if settings.ALLOWED_MTLS_SUBJECTS:
        client_cn = cert_info.get("common_name") or ""
        client_subj = cert_info.get("subject") or ""
        is_subject_allowed = any(
            allowed == client_cn or allowed == client_subj or allowed in client_subj
            for allowed in settings.ALLOWED_MTLS_SUBJECTS
        )
        if not is_subject_allowed:
            log_warning("mTLS", f"Отказ в доступе: субъект сертификата '{client_subj}' (CN: '{client_cn}') не входит в белый список")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Клиентский сертификат mTLS не авторизован (субъект: {client_cn or client_subj})"
            )

    # Проверка разрешенных эмитентов (CA)
    if settings.ALLOWED_MTLS_ISSUERS:
        client_issuer = cert_info.get("issuer") or ""
        is_issuer_allowed = any(
            allowed in client_issuer for allowed in settings.ALLOWED_MTLS_ISSUERS
        )
        if not is_issuer_allowed:
            log_warning("mTLS", f"Отказ в доступе: эмитент сертификата '{client_issuer}' не входит в белый список доверенных CA")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Эмитент клиентского сертификата mTLS не входит в список доверенных центров авторизации"
            )

    return cert_info




