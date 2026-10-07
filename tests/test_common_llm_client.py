"""
Тесты для модуля common.llm.client: проверка устойчивости к сбоям (Retry on RateLimitError 429).
"""

import pytest
from unittest.mock import patch, MagicMock
try:
    import litellm
    RateLimitError = getattr(litellm, "RateLimitError", None)
    if RateLimitError is None or isinstance(RateLimitError, MagicMock):
        from litellm.exceptions import RateLimitError
except (ImportError, AttributeError):
    class RateLimitError(Exception):
        def __init__(self, message="Resource exhausted", model="gemini", llm_provider="vertex_ai"):
            super().__init__(message)
            self.status_code = 429
            self.message = message

from src.common.llm import LLMClient


def test_llm_client_initialization():
    client = LLMClient(
        vertex_project="test-project",
        vertex_location="europe-west1",
        vertex_base_url="https://gateway.example.com",
        max_retries=3,
        initial_backoff=0.01,
    )
    assert client.vertex_project == "test-project"
    assert client.vertex_location == "europe-west1"
    assert client.vertex_base_url == "https://gateway.example.com"
    assert client.max_retries == 3
    assert client.get_vertex_api_base("vertex_ai/gemini-2.0-flash") == (
        "https://gateway.example.com/v1/projects/test-project/locations/europe-west1/publishers/google/models/gemini-2.0-flash"
    )


def test_llm_client_transient_error_detection():
    client = LLMClient()
    rate_err = RateLimitError(
        message="Resource exhausted",
        model="gemini",
        llm_provider="vertex_ai"
    )
    assert client._is_transient_error(rate_err) is True
    assert client._is_transient_error(ValueError("Invalid argument")) is False
    assert client._is_transient_error(Exception("HTTP 429 Too Many Requests")) is True


def test_llm_client_sync_retry_success():
    client = LLMClient(
        vertex_project="test-proj",
        vertex_base_url="https://gateway.test",
        max_retries=3,
        initial_backoff=0.01,
    )

    call_count = 0

    def mock_litellm_completion(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise RateLimitError(
                message="RESOURCE_EXHAUSTED",
                model="gemini",
                llm_provider="vertex_ai"
            )
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="Success!"))]
        return mock_resp

    with patch.object(litellm, "completion", side_effect=mock_litellm_completion):
        res = client.completion(
            model="vertex_ai/gemini-flash",
            messages=[{"role": "user", "content": "hi"}]
        )
        assert res.choices[0].message.content == "Success!"
        assert call_count == 3


def test_llm_client_async_retry_success():
    import asyncio
    client = LLMClient(
        vertex_project="test-proj",
        vertex_base_url="https://gateway.test",
        max_retries=3,
        initial_backoff=0.01,
    )

    call_count = 0

    async def mock_litellm_acompletion(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise RateLimitError(
                message="Resource exhausted 429",
                model="gemini",
                llm_provider="vertex_ai"
            )
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="Async Success!"))]
        return mock_resp

    with patch.object(litellm, "acompletion", side_effect=mock_litellm_acompletion):
        res = asyncio.run(client.acompletion(
            model="vertex_ai/gemini-flash",
            messages=[{"role": "user", "content": "hi"}]
        ))
        assert res.choices[0].message.content == "Async Success!"
        assert call_count == 2

