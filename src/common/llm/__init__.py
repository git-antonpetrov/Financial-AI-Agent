"""
Пакет корпоративного шлюза к языковым моделям и моделям векторных представлений.
Предоставляет централизованный клиент с поддержкой Cloudflare Gateway,
Vertex AI, семафоров параллелизма и автоматических повторов при лимитах квот (HTTP 429).
"""

from .client import LLMClient, default_llm_client

__all__ = [
    "LLMClient",
    "default_llm_client",
]
