import os
import uuid
from datetime import datetime, timedelta, timezone
import jwt
import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
import redis  # type: ignore
from core.utils.console_logger import log_error, log_warning

class Settings:
    SECRET_KEY: str = os.environ["JWT_SECRET_KEY"]
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 12 * 60  # 12 часов для десктопного приложения
    ADMIN_PASSWORD_HASH: str = os.getenv("ADMIN_PASSWORD_HASH", "")
    
    AGENT_DIGITAL_BOOTSTRAP_TOKEN: str = os.environ["AGENT_DIGITAL_BOOTSTRAP_TOKEN"]
    AGENT_BANK_BOOTSTRAP_TOKEN: str = os.environ["AGENT_BANK_BOOTSTRAP_TOKEN"]
    AGENT_INVEST_BOOTSTRAP_TOKEN: str = os.environ["AGENT_INVEST_BOOTSTRAP_TOKEN"]
    AGENT_MAIN_BOOTSTRAP_TOKEN: str = os.environ["AGENT_MAIN_BOOTSTRAP_TOKEN"]

    def get_bootstrap_token(self, agent_name: str) -> str:
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

try:
    redis_blacklist = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        decode_responses=True,
        socket_timeout=2.0,
        socket_connect_timeout=2.0
    )
except Exception as e:
    log_error("Auth", f"Failed to initialize Redis blacklist client: {e}")
    redis_blacklist = None

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))
    except Exception:
        return False

def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({
        "iat": now,
        "exp": expire,
        "jti": str(uuid.uuid4())
    })
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt

async def get_current_admin(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        username: str = payload.get("sub")
        if username != "admin":
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
            log_error("Auth", f"Security Alert: Redis blacklist lookup error: {e}")
            if os.getenv("AUTH_FAIL_CLOSED", "false").lower() == "true":
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Authentication service temporarily unavailable",
                    headers={"WWW-Authenticate": "Bearer"},
                )
    else:
        log_warning("Auth", "Redis blacklist is uninitialized; cannot verify token revocation")
            
    if is_revoked:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return username

def verify_agent_jwt(token: str, public_key: str) -> dict | None:
    try:
        # Проверяем подпись токена асимметричным публичным ключом RS256
        payload = jwt.decode(token, public_key, algorithms=["RS256"])
        return payload
    except Exception as e:
        log_warning("Auth", f"Agent JWT verification failed: {e}")
        return None
