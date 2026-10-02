import pytest
import asyncio
from unittest.mock import AsyncMock, patch

from src.simulations.core.utils.db_utils import validate_db_name
from src.simulations.db.bank.scripts.seed_db import create_database_if_not_exists as bank_create_db
from src.simulations.db.invest.scripts.seed_db import create_database_if_not_exists as invest_create_db
from src.simulations.db.digital_ruble.scripts.seed_db import create_database_if_not_exists as ruble_create_db


# ============================================================================
# ТЕСТЫ ВАЛИДАЦИИ И ЗАЩИТЫ ОТ SQL-ИНЪЕКЦИЙ В ИМЕНАХ БАЗ ДАННЫХ
# ============================================================================

def test_validate_db_name_accepts_valid_identifiers():
    """Проверяет, что валидные идентификаторы PostgreSQL успешно проходят проверку."""
    valid_names = [
        "bank_db",
        "invest_db",
        "ruble_db",
        "test_123",
        "db",
        "a" * 63  # Максимальная длина в PostgreSQL
    ]
    for name in valid_names:
        assert validate_db_name(name) == name


@pytest.mark.parametrize("malicious_name", [
    "bank_db; DROP DATABASE postgres; --",
    "bank_db' OR '1'='1",
    'bank_db" OR 1=1',
    "bank db",
    "bank-db",
    "bank.db",
    "bank$db",
    "bank/db",
    "../bank_db",
    "",
    "   ",
    "a" * 64  # Превышение длины PostgreSQL identifier (63 символа)
])
def test_validate_db_name_rejects_sql_injections_and_invalid_chars(malicious_name):
    """Проверяет, что любые инъекции, спецсимволы и недопустимые длины отклоняются с ValueError."""
    with pytest.raises(ValueError, match="Недопустимое имя базы данных|не может быть пустым"):
        validate_db_name(malicious_name)


def test_validate_db_name_rejects_non_string():
    """Проверяет, что нестроковые аргументы отклоняются."""
    with pytest.raises(ValueError, match="должно быть строкой"):
        validate_db_name(123)  # pyrefly: ignore [arg-type]


def test_seeders_create_database_prevents_sql_injection(monkeypatch):
    """
    Проверяет, что при попытке подсунуть инъекцию в DATABASE_URL
    функция create_database_if_not_exists во всех сидерах выбрасывает исключение
    и не выполняет опасный SQL-запрос.
    """
    async def _test():
        malicious_url = "postgresql+asyncpg://user:pass@localhost:5432/bank_db;DROP%20TABLE%20accounts;--"

        # 1. Bank seeder
        monkeypatch.setenv("BANK_DATABASE_URL", malicious_url)
        with patch("asyncpg.connect", new_callable=AsyncMock) as mock_connect:
            with pytest.raises(ValueError):
                await bank_create_db()
            mock_connect.assert_not_called()

        # 2. Invest seeder
        monkeypatch.setenv("INVEST_DATABASE_URL", malicious_url)
        with patch("asyncpg.connect", new_callable=AsyncMock) as mock_connect:
            with pytest.raises(ValueError):
                await invest_create_db()
            mock_connect.assert_not_called()

        # 3. Ruble seeder
        monkeypatch.setenv("RUBLE_DATABASE_URL", malicious_url)
        with patch("asyncpg.connect", new_callable=AsyncMock) as mock_connect:
            with pytest.raises(ValueError):
                await ruble_create_db()
            mock_connect.assert_not_called()

    asyncio.run(_test())


def test_seeders_create_database_uses_safe_quoted_identifier(monkeypatch):
    """
    Проверяет, что при валидном имени базы формируется безопасный DDL-запрос
    с экранированием двойными кавычками CREATE DATABASE "name".
    """
    async def _test():
        valid_url = "postgresql+asyncpg://user:pass@localhost:5432/my_safe_bank_db"
        monkeypatch.setenv("BANK_DATABASE_URL", valid_url)

        mock_conn = AsyncMock()
        with patch("asyncpg.connect", new_callable=AsyncMock, return_value=mock_conn):
            await bank_create_db()

        mock_conn.execute.assert_called_once_with('CREATE DATABASE "my_safe_bank_db"')
        mock_conn.close.assert_called_once()

    asyncio.run(_test())
