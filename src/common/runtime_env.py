"""Загружает локальный файл .env только вне тестового процесса."""

import os

from dotenv import load_dotenv


def load_runtime_dotenv() -> None:
    """Читает .env для боевого процесса и пропускает загрузку в тестах.

    Побочные эффекты:
        Вызывает load_dotenv, если FINANCIAL_AI_ALLOW_TEST_BRIDGES не включён.
    """
    if os.getenv("FINANCIAL_AI_ALLOW_TEST_BRIDGES", "").lower() in ("1", "true", "yes"):
        return
    load_dotenv()
