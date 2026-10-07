"""
Модуль корпоративного клиента LLM с поддержкой Google Vertex AI, Cloudflare Gateway
и автоматической обработкой квот и сбоев (Transient Faults: HTTP 429, 503).
"""

import os
import time
import random
import asyncio
import threading
from typing import Any, Optional, Union

import litellm

try:
    from src.common.logger import log_info, log_warning, log_error
except ImportError:
    try:
        from common.logger import log_info, log_warning, log_error
    except ImportError:
        def log_info(p: str, m: str) -> None: print(f"[Info][{p}]: {m}", flush=True)
        def log_warning(p: str, m: str) -> None: print(f"[Warning][{p}]: {m}", flush=True)
        def log_error(p: str, m: str) -> None: print(f"[Error][{p}]: {m}", flush=True)


class LLMClient:
    """
    Инфраструктурный фасад для взаимодействия с LLM и моделями эмбеддингов через LiteLLM.
    Обеспечивает централизованную настройку роутинга через Cloudflare Gateway,
    ограничение параллелизма и автоматические повторные попытки с Exponential Backoff и Full Jitter.
    """

    def __init__(
        self,
        vertex_project: Optional[str] = None,
        vertex_location: Optional[str] = None,
        vertex_base_url: Optional[str] = None,
        max_retries: Optional[int] = None,
        initial_backoff: float = 2.0,
        max_backoff: float = 60.0,
        backoff_factor: float = 2.0,
        concurrency_limit: int = 3,
    ) -> None:
        """
        Инициализирует клиент LLM параметрами окружения или переданными значениями.
        """
        self.vertex_project = vertex_project or os.getenv("VERTEX_PROJECT")
        self.vertex_location = vertex_location or os.getenv("VERTEX_LOCATION", "global")
        raw_base_url = vertex_base_url or os.getenv("VERTEX_BASE_URL", "")
        self.vertex_base_url = raw_base_url.rstrip("/") if raw_base_url else None

        env_retries = os.getenv("LLM_MAX_RETRIES")
        if max_retries is not None:
            self.max_retries = max_retries
        elif env_retries is not None and env_retries.isdigit():
            self.max_retries = int(env_retries)
        else:
            self.max_retries = 5

        self.initial_backoff = initial_backoff
        self.max_backoff = max_backoff
        self.backoff_factor = backoff_factor
        self.concurrency_limit = concurrency_limit

        self._async_semaphore: Optional[asyncio.Semaphore] = None
        self._sync_semaphore = threading.Semaphore(self.concurrency_limit)
        self._lock = threading.Lock()

    def _get_async_semaphore(self) -> asyncio.Semaphore:
        """Ленивая инициализация асинхронного семафора в контексте активного event loop."""
        if self._async_semaphore is None:
            with self._lock:
                if self._async_semaphore is None:
                    self._async_semaphore = asyncio.Semaphore(self.concurrency_limit)
        return self._async_semaphore

    def get_vertex_api_base(self, model_name: str) -> Optional[str]:
        """
        Формирует базовый URL запроса к модели Vertex AI через прокси Cloudflare.
        """
        if not self.vertex_base_url or not self.vertex_project:
            return None
        clean_model = model_name.replace("vertex_ai/", "")
        return (
            f"{self.vertex_base_url}/v1/projects/{self.vertex_project}"
            f"/locations/{self.vertex_location}/publishers/google/models/{clean_model}"
        )

    def _is_transient_error(self, error: Exception) -> bool:
        """
        Определяет, относится ли исключение к категории временных сбоев (429 Rate Limit, 503 Service Unavailable).
        """
        rate_types = []
        for attr in ("RateLimitError", "ServiceUnavailableError"):
            val = getattr(litellm, attr, None)
            if isinstance(val, type) and issubclass(val, BaseException):
                rate_types.append(val)

        if rate_types and isinstance(error, tuple(rate_types)):
            return True

        status_code = getattr(error, "status_code", None)
        if status_code in (429, 503):
            return True

        err_str = str(error)
        transient_indicators = (
            "429",
            "503",
            "RESOURCE_EXHAUSTED",
            "Resource exhausted",
            "RateLimitError",
            "rate_limit",
            "ServiceUnavailable",
            "ResourceExhausted",
        )
        return any(indicator in err_str for indicator in transient_indicators)

    def _calculate_backoff(self, attempt: int) -> float:
        """
        Вычисляет интервал задержки с использованием экспоненциального бэкоффа и Full Jitter.
        """
        raw_backoff = min(self.max_backoff, self.initial_backoff * (self.backoff_factor ** attempt))
        # Full Jitter: равномерное распределение от 50% до 100% от расчетного времени
        jittered_delay = random.uniform(raw_backoff * 0.5, raw_backoff)
        return round(jittered_delay, 2)

    def _prepare_kwargs(self, model: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        """
        Дополняет параметры вызова обязательными метаданными Vertex AI, если они не заданы явно.
        """
        prepared = kwargs.copy()
        if "api_base" not in prepared:
            api_base = self.get_vertex_api_base(model)
            if api_base:
                prepared["api_base"] = api_base

        if "vertex_project" not in prepared and self.vertex_project:
            prepared["vertex_project"] = self.vertex_project

        if "vertex_location" not in prepared and self.vertex_location:
            prepared["vertex_location"] = self.vertex_location

        return prepared

    async def acompletion(self, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        """
        Асинхронный вызов генерации текста (LLM completion) с семафором параллелизма
        и автоматическими повторами при ошибках 429/503.
        """
        call_kwargs = self._prepare_kwargs(model, kwargs)
        sem = self._get_async_semaphore()

        async with sem:
            for attempt in range(self.max_retries + 1):
                try:
                    return await litellm.acompletion(model=model, messages=messages, **call_kwargs)
                except Exception as e:
                    if attempt < self.max_retries and self._is_transient_error(e):
                        delay = self._calculate_backoff(attempt)
                        log_warning(
                            "LLM Resilience",
                            f"Получена ошибка квоты/сервиса ({type(e).__name__}). "
                            f"Попытка {attempt + 1}/{self.max_retries}. Ожидание {delay} сек..."
                        )
                        await asyncio.sleep(delay)
                    else:
                        log_error("LLM Resilience", f"Критическая ошибка вызова acompletion для {model}: {e}")
                        raise

    def completion(self, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        """
        Синхронный вызов генерации текста (LLM completion) с семафором параллелизма
        и автоматическими повторами при ошибках 429/503.
        """
        call_kwargs = self._prepare_kwargs(model, kwargs)

        with self._sync_semaphore:
            for attempt in range(self.max_retries + 1):
                try:
                    return litellm.completion(model=model, messages=messages, **call_kwargs)
                except Exception as e:
                    if attempt < self.max_retries and self._is_transient_error(e):
                        delay = self._calculate_backoff(attempt)
                        log_warning(
                            "LLM Resilience",
                            f"Получена ошибка квоты/сервиса ({type(e).__name__}). "
                            f"Попытка {attempt + 1}/{self.max_retries}. Ожидание {delay} сек..."
                        )
                        time.sleep(delay)
                    else:
                        log_error("LLM Resilience", f"Критическая ошибка вызова completion для {model}: {e}")
                        raise

    async def aembedding(self, model: str, input: Union[str, list[str]], **kwargs: Any) -> Any:
        """
        Асинхронный вызов создания эмбеддингов с автоматическими повторами при 429/503.
        """
        input_list = [input] if isinstance(input, str) else input
        call_kwargs = self._prepare_kwargs(model, kwargs)
        sem = self._get_async_semaphore()

        async with sem:
            for attempt in range(self.max_retries + 1):
                try:
                    return await litellm.aembedding(model=model, input=input_list, **call_kwargs)
                except Exception as e:
                    if attempt < self.max_retries and self._is_transient_error(e):
                        delay = self._calculate_backoff(attempt)
                        log_warning(
                            "LLM Resilience",
                            f"Получена ошибка квоты при aembedding. "
                            f"Попытка {attempt + 1}/{self.max_retries}. Ожидание {delay} сек..."
                        )
                        await asyncio.sleep(delay)
                    else:
                        log_error("LLM Resilience", f"Критическая ошибка aembedding для {model}: {e}")
                        raise

    def embedding(self, model: str, input: Union[str, list[str]], **kwargs: Any) -> Any:
        """
        Синхронный вызов создания эмбеддингов с автоматическими повторами при 429/503.
        """
        input_list = [input] if isinstance(input, str) else input
        call_kwargs = self._prepare_kwargs(model, kwargs)

        with self._sync_semaphore:
            for attempt in range(self.max_retries + 1):
                try:
                    return litellm.embedding(model=model, input=input_list, **call_kwargs)
                except Exception as e:
                    if attempt < self.max_retries and self._is_transient_error(e):
                        delay = self._calculate_backoff(attempt)
                        log_warning(
                            "LLM Resilience",
                            f"Получена ошибка квоты при embedding. "
                            f"Попытка {attempt + 1}/{self.max_retries}. Ожидание {delay} сек..."
                        )
                        time.sleep(delay)
                    else:
                        log_error("LLM Resilience", f"Критическая ошибка embedding для {model}: {e}")
                        raise

    def get_embeddings_batch(
        self,
        model: str,
        texts: list[str],
        sleep_between_items: float = 0.0,
        **kwargs: Any
    ) -> list[list[float]]:
        """
        Последовательно генерирует эмбеддинги для списка текстов с защитой от ошибок размерности
        и автоматическим перезапуском при превышении квот.
        """
        embeddings: list[list[float]] = []
        for text in texts:
            response = self.embedding(model=model, input=[text], **kwargs)
            embeddings.append(response.data[0]["embedding"])
            if sleep_between_items > 0:
                time.sleep(sleep_between_items)
        return embeddings


# Глобальный синглтон-экземпляр по умолчанию
default_llm_client = LLMClient()
