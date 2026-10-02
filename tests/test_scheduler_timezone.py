import inspect
from pathlib import Path
import yaml
from unittest.mock import patch, MagicMock

import src.simulations.workers.bank_eod.main as bank_main
import src.simulations.workers.invest_eod.main as invest_main
import src.simulations.workers.digital_eod.main as digital_main


def test_bank_eod_main_configures_moscow_timezone():
    """
    Проверяет, что bank_eod инициализирует AsyncIOScheduler с часовым поясом 'Europe/Moscow'
    и добавляет задачу с указанием московской таймзоны.
    """
    with patch("src.simulations.workers.bank_eod.main.AsyncIOScheduler") as mock_scheduler_cls, \
         patch("asyncio.Event") as mock_event:
        mock_scheduler = MagicMock()
        mock_scheduler_cls.return_value = mock_scheduler

        # Настраиваем mock event, чтобы не зависать в бесконечном wait()
        mock_wait = MagicMock()
        async def fake_wait():
            pass
        mock_event.return_value.wait = fake_wait

        import asyncio
        asyncio.run(bank_main.main())

        # Проверяем инициализацию AsyncIOScheduler(timezone='Europe/Moscow')
        mock_scheduler_cls.assert_called_once()
        _, kwargs = mock_scheduler_cls.call_args
        assert kwargs.get("timezone") == "Europe/Moscow"

        # Проверяем добавление задачи в 23:30 с таймзоной Europe/Moscow
        mock_scheduler.add_job.assert_called_once()
        _, job_kwargs = mock_scheduler.add_job.call_args
        assert job_kwargs.get("hour") == 23
        assert job_kwargs.get("minute") == 30
        assert job_kwargs.get("timezone") == "Europe/Moscow"


def test_invest_eod_main_configures_moscow_timezone():
    """
    Проверяет, что invest_eod инициализирует AsyncIOScheduler с часовым поясом 'Europe/Moscow'.
    """
    with patch("src.simulations.workers.invest_eod.main.AsyncIOScheduler") as mock_scheduler_cls, \
         patch("asyncio.Event") as mock_event:
        mock_scheduler = MagicMock()
        mock_scheduler_cls.return_value = mock_scheduler

        async def fake_wait():
            pass
        mock_event.return_value.wait = fake_wait

        import asyncio
        asyncio.run(invest_main.main())

        mock_scheduler_cls.assert_called_once()
        _, kwargs = mock_scheduler_cls.call_args
        assert kwargs.get("timezone") == "Europe/Moscow"

        mock_scheduler.add_job.assert_called_once()
        _, job_kwargs = mock_scheduler.add_job.call_args
        assert job_kwargs.get("hour") == 23
        assert job_kwargs.get("minute") == 15
        assert job_kwargs.get("timezone") == "Europe/Moscow"


def test_digital_eod_main_configures_moscow_timezone():
    """
    Проверяет, что digital_eod инициализирует AsyncIOScheduler с часовым поясом 'Europe/Moscow'.
    """
    with patch("src.simulations.workers.digital_eod.main.AsyncIOScheduler") as mock_scheduler_cls, \
         patch("asyncio.Event") as mock_event:
        mock_scheduler = MagicMock()
        mock_scheduler_cls.return_value = mock_scheduler

        async def fake_wait():
            pass
        mock_event.return_value.wait = fake_wait

        import asyncio
        asyncio.run(digital_main.main())

        mock_scheduler_cls.assert_called_once()
        _, kwargs = mock_scheduler_cls.call_args
        assert kwargs.get("timezone") == "Europe/Moscow"

        mock_scheduler.add_job.assert_called_once()
        _, job_kwargs = mock_scheduler.add_job.call_args
        assert job_kwargs.get("hour") == 23
        assert job_kwargs.get("minute") == 45
        assert job_kwargs.get("timezone") == "Europe/Moscow"


def test_docker_compose_worker_tz_env():
    """
    Проверяет, что в docker-compose.yml для всех воркеров задана переменная TZ=Europe/Moscow.
    """
    compose_path = Path(__file__).parent.parent / "docker-compose.yml"
    with open(compose_path, "r", encoding="utf-8") as f:
        compose_data = yaml.safe_load(f)

    services = compose_data["services"]
    workers = ["bank-worker", "invest-worker", "digital-worker"]
    for w in workers:
        assert w in services, f"Сервис {w} отсутствует в docker-compose.yml"
        env = services[w].get("environment", [])
        assert "TZ=Europe/Moscow" in env or any("Europe/Moscow" in str(e) for e in env), \
            f"Для {w} должна быть задана переменная окружения TZ=Europe/Moscow"
