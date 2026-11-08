import asyncio
import sys
from pathlib import Path

# Добавляет корень проекта в sys.path.
sys.path.append(str(Path(__file__).resolve().parents[4]))

from dotenv import load_dotenv

load_dotenv()

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.simulations.core.utils.console_logger import log_info
from src.simulations.workers.bank_eod.tasks import run_bank_eod


async def main():
    log_info("Bank EOD Worker", "Инициализация воркера...")

    # Создаёт асинхронный планировщик в часовом поясе Москвы.
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

    # Настраивает запуск EOD каждый день в 23:30 по Москве
    scheduler.add_job(run_bank_eod, "cron", hour=23, minute=30, timezone="Europe/Moscow")

    # Запускает планировщик
    scheduler.start()
    log_info("Bank EOD Worker", "Воркер запущен и работает в фоне. Ожидание расписания...")

    # Элегантный способ держать процесс открытым без while True
    # Создаёт Future, которое не завершается, потому что процесс должен оставаться запущенным.
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log_info("Bank EOD Worker", "Воркер остановлен.")
