import litellm
from functools import lru_cache
# pyrefly: ignore [missing-import]
from chromadb import EmbeddingFunction
# pyrefly: ignore [missing-import]
from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction

from src.core.config import get_settings
from src.core.utils.console_logger import log_info

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
        settings = get_settings()
        BATCH_SIZE = 200
        all_embeddings = []
        for i in range(0, len(input), BATCH_SIZE):
            batch = input[i:i + BATCH_SIZE]
            
            kwargs = {
                "model": "vertex_ai/gemini-embedding-001",
                "input": batch
            }
            if settings.VERTEX_API_BASE:
                # litellm ожидает, что api_base будет полным URL до модели, иначе он ломает путь
                location = settings.VERTEX_LOCATION or "global"
                project = settings.VERTEX_PROJECT
                model_clean = "gemini-embedding-001"
                base = settings.VERTEX_API_BASE.rstrip('/')
                kwargs["api_base"] = f"{base}/v1/projects/{project}/locations/{location}/publishers/google/models/{model_clean}"
            if settings.VERTEX_LOCATION:
                kwargs["vertex_location"] = settings.VERTEX_LOCATION
            if settings.VERTEX_PROJECT:
                kwargs["vertex_project"] = settings.VERTEX_PROJECT
                
            response = litellm.embedding(**kwargs)
            all_embeddings.extend([item['embedding'] for item in response['data']])
        return all_embeddings

@lru_cache
def get_embedder() -> EmbeddingFunction:
    """
    Создает и возвращает функцию эмбеддинга в зависимости от настроек окружения.
    
    Returns:
        EmbeddingFunction: Функция для векторизации текста.
        
    Raises:
        ValueError: Если указан неизвестный провайдер.
    """
    settings = get_settings()
    provider = settings.EMBEDDING_PROVIDER.lower()
    
    log_info("LLM", "Инициализация функции эмбеддинга...")
    if provider == "vertex":
        return LiteLLMVertexEmbeddingFunction()
    elif provider == "openai":
        return OpenAIEmbeddingFunction(
            api_key=settings.OPENAI_API_KEY,
            model_name="text-embedding-3-small"
        )
    else:
        raise ValueError(f"Неизвестный провайдер эмбеддингов: {provider}")

class CloudAIClient:
    """
    Универсальная обертка над LLM-провайдером (через litellm) для работы с облачными моделями.
    """
    def __init__(self):
        self.settings = get_settings()

    def completion(self, *args, **kwargs):
        """Синхронный вызов LLM с автоматической подстановкой параметров проекта."""
        if "vertex_location" not in kwargs and self.settings.VERTEX_LOCATION:
            kwargs["vertex_location"] = self.settings.VERTEX_LOCATION
        if "vertex_project" not in kwargs and self.settings.VERTEX_PROJECT:
            kwargs["vertex_project"] = self.settings.VERTEX_PROJECT
        if "api_base" not in kwargs and self.settings.VERTEX_API_BASE:
            location = kwargs["vertex_location"]
            project = kwargs["vertex_project"]
            model = args[0] if args else kwargs.get("model", "")
            model_clean = model.replace("vertex_ai/", "")
            base = self.settings.VERTEX_API_BASE.rstrip('/')
            kwargs["api_base"] = f"{base}/v1/projects/{project}/locations/{location}/publishers/google/models/{model_clean}"
        return litellm.completion(*args, **kwargs)

    async def acompletion(self, *args, **kwargs):
        """Асинхронный вызов LLM с автоматической подстановкой параметров проекта."""
        if "vertex_location" not in kwargs and self.settings.VERTEX_LOCATION:
            kwargs["vertex_location"] = self.settings.VERTEX_LOCATION
        if "vertex_project" not in kwargs and self.settings.VERTEX_PROJECT:
            kwargs["vertex_project"] = self.settings.VERTEX_PROJECT
        if "api_base" not in kwargs and self.settings.VERTEX_API_BASE:
            location = kwargs["vertex_location"]
            project = kwargs["vertex_project"]
            model = args[0] if args else kwargs.get("model", "")
            model_clean = model.replace("vertex_ai/", "")
            base = self.settings.VERTEX_API_BASE.rstrip('/')
            kwargs["api_base"] = f"{base}/v1/projects/{project}/locations/{location}/publishers/google/models/{model_clean}"
        return await litellm.acompletion(*args, **kwargs)

@lru_cache
def get_cloud_ai_client() -> CloudAIClient:
    """
    Возвращает экземпляр клиента CloudAIClient для работы с LLM.
    Использует кэширование для сохранения единственного экземпляра (Singleton).
    """
    log_info("LLM", "Инициализация Cloud AI Client...")
    return CloudAIClient()
