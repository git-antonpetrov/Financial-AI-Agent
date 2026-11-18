"""Проверяет маскирование номера карты и отказ генератора при сплошных коллизиях."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from src.simulations.api.routers.bank import generate_unique_card_number, mask_card_number


def test_mask_card_number_keeps_edges_of_full_pan():
    """Проверяет, что номер от восьми знаков оставляет видимыми первые и последние четыре."""
    assert mask_card_number("4276123456789012") == "4276********9012"
    assert mask_card_number("12345678") == "1234********5678"


def test_mask_card_number_hides_short_value():
    """Проверяет, что короткий номер не раскрывает ни одного знака."""
    assert mask_card_number("1234567") == "****"
    assert mask_card_number("") == "****"


def test_generate_unique_card_number_returns_free_candidate():
    """Проверяет, что свободный кандидат возвращается как 16-значный номер с префиксом 4276."""

    async def _run():
        db = MagicMock()
        db.scalar = AsyncMock(return_value=None)
        number = await generate_unique_card_number(db)
        assert len(number) == 16
        assert number.startswith("4276")
        assert number.isdigit()
        db.scalar.assert_awaited()

    asyncio.run(_run())


def test_generate_unique_card_number_rejects_exhausted_collisions():
    """Проверяет, что занятые номера заканчиваются отказом, а не повтором чужой карты."""

    async def _run():
        db = MagicMock()
        db.scalar = AsyncMock(return_value="existing-card")
        with pytest.raises(HTTPException) as captured:
            await generate_unique_card_number(db, max_attempts=3)
        assert captured.value.status_code == 500
        assert db.scalar.await_count == 3

    asyncio.run(_run())
