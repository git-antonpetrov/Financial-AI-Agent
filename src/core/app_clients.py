import os
from typing import Any
from dotenv import load_dotenv
import litellm

# Load environment variables
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

def get_chroma_client():
    """Инициализирует и возвращает клиент векторной базы данных."""
    host = os.getenv("CHROMA_HOST", "localhost")
    port = int(os.getenv("CHROMA_PORT", "8000"))
    return chromadb.HttpClient(host=host, port=port)

class LiteLLMVertexEmbeddingFunction(EmbeddingFunction):
    def __call__(self, input: list[str]):
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
    """Создает функцию эмбеддинга в зависимости от настроек."""
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
    """Универсальная обертка над LLM-провайдером (через litellm)."""
    
    def completion(self, *args, **kwargs):
        if "vertex_location" not in kwargs:
            kwargs["vertex_location"] = os.getenv("VERTEX_LOCATION", "global")
        if "vertex_project" not in kwargs:
            kwargs["vertex_project"] = os.getenv("VERTEX_PROJECT")
        return litellm.completion(*args, **kwargs)

    async def acompletion(self, *args, **kwargs):
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

    @classmethod
    def get_embedder_client(cls) -> Any:
        if cls._embedder is None:
            print("System: \033[96m[Инициализация]\033[0m Загрузка эмбеддера...")
            cls._embedder = get_embedder()
        return cls._embedder

    @classmethod
    def get_chroma_db(cls) -> Any:
        if cls._chroma_client is None:
            print("System: \033[96m[Инициализация]\033[0m Подключение к ChromaDB...")
            cls._chroma_client = get_chroma_client()
        return cls._chroma_client

    @classmethod
    def get_minio_client(cls) -> Any:
        if cls._minio_client is None:
            print("System: \033[96m[Инициализация]\033[0m Подключение к MinIO...")
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
        if cls._content_ai_client is None:
            print("System: \033[96m[Инициализация]\033[0m Инициализация Content AI...")
            cls._content_ai_client = ContentCaptureRecognizer(delete_batch_after=True)
        return cls._content_ai_client

    @classmethod
    def get_cloud_ai_client(cls) -> Any:
        if cls._cloud_ai_client is None:
            print("System: \033[96m[Инициализация]\033[0m Инициализация Cloud AI Client...")
            cls._cloud_ai_client = CloudAIClient()
        return cls._cloud_ai_client
