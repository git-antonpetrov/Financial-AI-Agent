import os
from typing import Any
from dotenv import load_dotenv
import litellm

# Загружает переменные окружения
load_dotenv()
# pyrefly: ignore [missing-import]
import chromadb
# pyrefly: ignore [missing-import]
from chromadb import EmbeddingFunction
# pyrefly: ignore [missing-import]
from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction
# pyrefly: ignore [missing-import]
from minio import Minio
from src.services.content_ai_recognizer import ContentCaptureRecognizer
from src.utils.console_logger import log_info, log_error
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine, async_sessionmaker, AsyncSession
from src.core.models import Base

def get_chroma_client():
    """
    Инициализирует и возвращает клиент векторной базы данных ChromaDB.
    Берет хост и порт из переменных окружения.
    """
    host = os.getenv("CHROMA_HOST", "localhost")
    port = int(os.getenv("CHROMA_PORT", "8000"))
    return chromadb.HttpClient(host=host, port=port)

class LiteLLMVertexEmbeddingFunction(EmbeddingFunction):
    """
    Класс для работы с эмбеддингами Vertex AI через библиотеку LiteLLM.
    """
    def __call__(self, input: list[str]):
        """
        Преобразует список текстов в список векторных эмбеддингов.
        
        Args:
            input (list[str]): Список текстовых строк для векторизации.
            
        Returns:
            list: Список полученных эмбеддингов.
        """
        BATCH_SIZE = 200
        all_embeddings = []
        for i in range(0, len(input), BATCH_SIZE):
            batch = input[i:i + BATCH_SIZE]
            response = litellm.embedding(
                model="vertex_ai/gemini-embedding-001",
                input=batch
            )
            all_embeddings.extend([item['embedding'] for item in response['data']])
        return all_embeddings

def get_embedder():
    """
    Создает и возвращает функцию эмбеддинга в зависимости от настроек окружения.
    
    Returns:
        EmbeddingFunction: Функция для векторизации текста.
        
    Raises:
        ValueError: Если указан неизвестный провайдер.
    """
    provider = os.getenv("EMBEDDING_PROVIDER", "vertex").lower()
    
    if provider == "vertex":
        return LiteLLMVertexEmbeddingFunction()
    elif provider == "openai":
        return OpenAIEmbeddingFunction(
            api_key=os.getenv("OPENAI_API_KEY"),
            model_name="text-embedding-3-small"
        )
    else:
        raise ValueError(f"Неизвестный провайдер эмбеддингов: {provider}")

class CloudAIClient:
    """
    Универсальная обертка над LLM-провайдером (через litellm) для работы с облачными моделями.
    """
    
    def completion(self, *args, **kwargs):
        """Синхронный вызов LLM с автоматической подстановкой параметров проекта."""
        if "vertex_location" not in kwargs:
            kwargs["vertex_location"] = os.getenv("VERTEX_LOCATION", "global")
        if "vertex_project" not in kwargs:
            kwargs["vertex_project"] = os.getenv("VERTEX_PROJECT")
        return litellm.completion(*args, **kwargs)

    async def acompletion(self, *args, **kwargs):
        """Асинхронный вызов LLM с автоматической подстановкой параметров проекта."""
        if "vertex_location" not in kwargs:
            kwargs["vertex_location"] = os.getenv("VERTEX_LOCATION", "global")
        if "vertex_project" not in kwargs:
            kwargs["vertex_project"] = os.getenv("VERTEX_PROJECT")
        return await litellm.acompletion(*args, **kwargs)

class AppClients:
    """Глобальный контейнер для ленивой загрузки (Lazy Loading) тяжелых клиентов."""
    _embedder = None
    _chroma_client = None
    _minio_client = None
    _content_ai_client = None
    _cloud_ai_client = None
    _db_engine: AsyncEngine | None = None
    _async_session_maker: async_sessionmaker[AsyncSession] | None = None

    @classmethod
    def get_embedder_client(cls) -> Any:
        """Возвращает клиента для генерации эмбеддингов."""
        if cls._embedder is None:
            log_info("Инициализация", "Загрузка эмбеддера...")
            cls._embedder = get_embedder()
        return cls._embedder

    @classmethod
    def get_chroma_db(cls) -> Any:
        """Возвращает клиента для работы с ChromaDB."""
        if cls._chroma_client is None:
            log_info("Инициализация", "Подключение к ChromaDB...")
            cls._chroma_client = get_chroma_client()
        return cls._chroma_client

    @classmethod
    def get_minio_client(cls) -> Any:
        """Возвращает клиента для работы с S3-хранилищем MinIO."""
        if cls._minio_client is None:
            log_info("Инициализация", "Подключение к MinIO...")
            endpoint = os.getenv("MINIO_ENDPOINT", "localhost:9000")
            access_key = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
            secret_key = os.getenv("MINIO_SECRET_KEY", "minioadmin")
            cls._minio_client = Minio(
                endpoint,
                access_key=access_key,
                secret_key=secret_key,
                secure=False
            )
        return cls._minio_client

    @classmethod
    def get_content_ai_client(cls) -> Any:
        """Возвращает клиента для работы с Content AI (OCR)."""
        if cls._content_ai_client is None:
            log_info("Инициализация", "Инициализация Content AI...")
            cls._content_ai_client = ContentCaptureRecognizer(delete_batch_after=True)
        return cls._content_ai_client

    @classmethod
    def get_cloud_ai_client(cls) -> Any:
        """Возвращает клиента для работы с LLM (Cloud AI)."""
        if cls._cloud_ai_client is None:
            log_info("Инициализация", "Инициализация Cloud AI Client...")
            cls._cloud_ai_client = CloudAIClient()
        return cls._cloud_ai_client

    @classmethod
    def get_db_engine(cls) -> AsyncEngine:
        """
        Ленивая инициализация движка базы данных PostgreSQL (asyncpg).
        Использует строку подключения DATABASE_URL из переменных окружения.
        """
        if cls._db_engine is None:
            db_url = os.getenv("DATABASE_URL")
            if not db_url:
                log_error("Система БД", "Переменная окружения DATABASE_URL не задана!")
                raise ValueError("DATABASE_URL must be set")
            
            # Создаем движок асинхронно
            cls._db_engine = create_async_engine(db_url, echo=False)
            cls._async_session_maker = async_sessionmaker(cls._db_engine, class_=AsyncSession, expire_on_commit=False)
            log_info("Система БД", "Асинхронный движок SQLAlchemy успешно инициализирован.")
            
        return cls._db_engine

    @classmethod
    def get_async_session(cls) -> async_sessionmaker[AsyncSession]:
        """
        Возвращает фабрику асинхронных сессий. При необходимости инициализирует движок.
        """
        if cls._async_session_maker is None:
            cls.get_db_engine()
        # Добавляем # type: ignore, чтобы линтер не ругался (мы уверены, что он проинициализирован)
        return cls._async_session_maker # type: ignore

    @classmethod
    async def init_db(cls):
        """
        Создает все таблицы в базе данных (если они еще не существуют).
        Должно вызываться один раз при старте приложения.
        """
        engine = cls.get_db_engine()
        log_info("Система БД", "Проверка и создание таблиц PostgreSQL...")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        log_info("Система БД", "Таблицы успешно проверены/созданы.")
