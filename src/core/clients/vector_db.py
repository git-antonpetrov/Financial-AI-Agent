from functools import lru_cache
# pyrefly: ignore [missing-import]
import chromadb
from chromadb.api import ClientAPI
from src.core.config import get_settings
from src.core.utils.console_logger import log_info

@lru_cache
def get_chroma_client() -> ClientAPI:
    """
    Инициализирует и возвращает клиент векторной базы данных ChromaDB.
    Берет хост и порт из централизованных настроек.
    Использует кэширование для сохранения единственного экземпляра (Singleton).
    
    Returns:
        ClientAPI: Экземпляр HTTP-клиента ChromaDB.
    """
    settings = get_settings()
    log_info("Векторная БД", "Подключение к ChromaDB...")
    
    client = chromadb.HttpClient(host=settings.CHROMA_HOST, port=settings.CHROMA_PORT)
    return client
