import os
import litellm
# pyrefly: ignore [missing-import]
from chromadb import EmbeddingFunction
# pyrefly: ignore [missing-import]
from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction

class LiteLLMVertexEmbeddingFunction(EmbeddingFunction):
    def __call__(self, input: list[str]):
        response = litellm.embedding(
            model="vertex_ai/gemini-embedding-001",
            input=input
        )
        return [item['embedding'] for item in response['data']]

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
