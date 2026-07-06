import os
from typing import Any
import litellm
# pyrefly: ignore [missing-import]
import chromadb
# pyrefly: ignore [missing-import]
from chromadb import EmbeddingFunction
# pyrefly: ignore [missing-import]
from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction

def get_chroma_client():
    """Инициализирует и возвращает клиент векторной базы данных."""
    return chromadb.HttpClient(host="localhost", port=8000)

class LiteLLMVertexEmbeddingFunction(EmbeddingFunction):
    def __call__(self, input: list[str]):
        # Vertex AI поддерживает максимум 250 текстов за один запрос.
        # Бьем входной массив на батчи по 200 элементов для надежности.
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

class AppClients:
    """Глобальный контейнер для ленивой загрузки (Lazy Loading) тяжелых клиентов."""
    _embedder = None
    _chroma_client = None

    @classmethod
    def get_embedder_client(cls) -> Any:
        # Если эмбеддер еще не создавали — создаем
        if cls._embedder is None:
            print("System: \033[96m[Инициализация]\033[0m Загрузка эмбеддера...")
            cls._embedder = get_embedder()
        return cls._embedder

    @classmethod
    def get_chroma_db(cls) -> Any:
        # Если клиент БД еще не создавали — создаем
        if cls._chroma_client is None:
            print("System: \033[96m[Инициализация]\033[0m Подключение к ChromaDB...")
            cls._chroma_client = get_chroma_client()
        return cls._chroma_client
