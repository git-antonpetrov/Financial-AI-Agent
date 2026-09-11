from functools import lru_cache
from minio import Minio
from src.core.config import get_settings
from src.utils.console_logger import log_info

@lru_cache
def get_minio_client() -> Minio:
    """
    Инициализирует и возвращает клиента для работы с S3-хранилищем (MinIO).
    Использует кэширование для сохранения единственного экземпляра (Singleton).
    
    Returns:
        Minio: Экземпляр клиента MinIO.
    """
    settings = get_settings()
    log_info("Система Хранения", "Подключение к MinIO...")
    
    client = Minio(
        endpoint=settings.MINIO_ENDPOINT,
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY,
        secure=False
    )
    return client
