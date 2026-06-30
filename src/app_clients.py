from typing import Any
# pyrefly: ignore [missing-import]
from config.embedding_config import get_embedder
# pyrefly: ignore [missing-import]
from config.chroma_config import get_chroma_client

class AppClients:
    """Глобальный контейнер для ленивой загрузки (Lazy Loading) тяжелых клиентов."""
    _embedder = None
    _chroma_client = None

    @classmethod
    def get_embedder_client(cls) -> Any:
        # Если эмбеддер еще не создавали — создаем
        if cls._embedder is None:
            print("System: \033[96m[Инициализация]\033[0m Загрузка эмбеддера Vertex AI...")
            cls._embedder = get_embedder()
        return cls._embedder

    @classmethod
    def get_chroma_db(cls) -> Any:
        # Если клиент БД еще не создавали — создаем
        if cls._chroma_client is None:
            print("System: \033[96m[Инициализация]\033[0m Подключение к ChromaDB...")
            cls._chroma_client = get_chroma_client()
        return cls._chroma_client
