from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional
from functools import lru_cache

class Settings(BaseSettings):
    """
    Класс для централизованной валидации и хранения настроек окружения.
    Использует Pydantic для проверки типов и обязательности полей.
    """
    
    # ChromaDB
    CHROMA_HOST: str = "localhost"
    CHROMA_PORT: int = 8000
    
    # LLM Settings (Vertex)
    VERTEX_PROJECT: Optional[str] = None
    VERTEX_LOCATION: str = "global"
    VERTEX_API_BASE: Optional[str] = None
    EMBEDDING_PROVIDER: str = "vertex"
    OPENAI_API_KEY: Optional[str] = None
    
    # Models
    MAIN_MODEL_NAME: str = "vertex_ai/gemini-3.1-pro-preview"
    MAIN_REASONING_EFFORT: str = "high"
    META_MODEL_NAME: str = "vertex_ai/gemini-3.5-flash"
    META_REASONING_EFFORT: str = "medium"
    FILE_NAME_MODEL_NAME: str = "vertex_ai/gemini-3.5-flash"
    FILE_NAME_REASONING_EFFORT: str = "medium"
    PIPELINE_MODEL_NAME: str = "vertex_ai/gemini-3.5-flash"
    PIPELINE_REASONING_EFFORT: str = "medium"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

@lru_cache
def get_settings() -> Settings:
    """
    Возвращает закешированный экземпляр настроек приложения.
    """
    return Settings()
