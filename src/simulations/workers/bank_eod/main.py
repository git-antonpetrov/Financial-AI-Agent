import os
import sys
import asyncio

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from dotenv import load_dotenv
load_dotenv()

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from src.simulations.workers.bank_eod.tasks import run_bank_eod
from src.core.utils.console_logger import log_info

async def main():
    log_info("Bank EOD Worker", "Инициализация воркера...")
    
    # Создаем асинхронный планировщик
    scheduler = AsyncIOScheduler()
    
    # Настраиваем запуск EOD каждый день в 23:30
    scheduler.add_job(run_bank_eod, 'cron', hour=23, minute=30)
    
    # Запускаем планировщик
    scheduler.start()
    log_info("Bank EOD Worker", "Воркер запущен и работает в фоне. Ожидание расписания...")
    
    # Элегантный способ держать процесс открытым без while True
    # Создаем Future, которое никогда не выполнится
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log_info("Bank EOD Worker", "Воркер остановлен.")
