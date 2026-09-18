from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional
from functools import lru_cache

class EltSettings(BaseSettings):
    """
    Класс для централизованной валидации и хранения настроек окружения для ELT.
    """
    
    # Database
    DATABASE_URL: str
    
    # MinIO
    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: str = "minioadmin"
    
    # Content AI
    CONTENT_AI_API_URI: Optional[str] = None
    CONTENTAI_USERNAME: Optional[str] = None
    CONTENTAI_PASSWORD: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

@lru_cache
def get_settings() -> EltSettings:
    """
    Возвращает закешированный экземпляр настроек ELT приложения.
    """
    return EltSettings()
