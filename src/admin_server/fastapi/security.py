import os
from datetime import datetime, timedelta, timezone
from jose import JWTError, jwt
import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
class Settings:
    SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "super-secret-key-please-change-in-env")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 30  # 30 дней
    ADMIN_PASSWORD_HASH: str = os.getenv("ADMIN_PASSWORD_HASH", "")
    
    AGENT_DIGITAL_BOOTSTRAP_TOKEN: str = os.getenv("AGENT_DIGITAL_BOOTSTRAP_TOKEN", "digital-secret-123")
    AGENT_BANK_BOOTSTRAP_TOKEN: str = os.getenv("AGENT_BANK_BOOTSTRAP_TOKEN", "bank-secret-456")
    AGENT_INVEST_BOOTSTRAP_TOKEN: str = os.getenv("AGENT_INVEST_BOOTSTRAP_TOKEN", "invest-secret-789")
    AGENT_MAIN_BOOTSTRAP_TOKEN: str = os.getenv("AGENT_MAIN_BOOTSTRAP_TOKEN", "main-secret-000")

    def get_bootstrap_token(self, agent_name: str) -> str:
        tokens = {
            "digital": self.AGENT_DIGITAL_BOOTSTRAP_TOKEN,
            "bank": self.AGENT_BANK_BOOTSTRAP_TOKEN,
            "invest": self.AGENT_INVEST_BOOTSTRAP_TOKEN,
            "main": self.AGENT_MAIN_BOOTSTRAP_TOKEN,
        }
        return tokens.get(agent_name)

settings = Settings()

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
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
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
    except JWTError:
        raise credentials_exception
    return username

def verify_agent_jwt(token: str, public_key: str) -> dict:
    try:
        # Проверяем подпись токена асимметричным публичным ключом RS256
        payload = jwt.decode(token, public_key, algorithms=["RS256"])
        return payload
    except JWTError:
        return None
