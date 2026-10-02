import sys
import asyncio
from pathlib import Path

# Добавляем корень проекта в sys.path
sys.path.append(str(Path(__file__).resolve().parents[4]))

from dotenv import load_dotenv
load_dotenv()

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from src.simulations.workers.digital_eod.tasks import run_ruble_eod
from src.simulations.core.utils.console_logger import log_info

async def main():
    log_info("Ruble EOD Worker", "Инициализация воркера смарт-контрактов...")
    
    # Создаем асинхронный планировщик с явным указанием московского времени
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    
    # Настраиваем запуск EOD каждый день в 23:45 по Москве
    scheduler.add_job(run_ruble_eod, 'cron', hour=23, minute=45, timezone="Europe/Moscow")
    
    scheduler.start()
    log_info("Ruble EOD Worker", "Воркер цифрового рубля запущен. Ожидание 23:45...")
    
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log_info("Ruble EOD Worker", "Воркер остановлен.")
