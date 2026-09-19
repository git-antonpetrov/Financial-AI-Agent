import os
import sys
import asyncio

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from dotenv import load_dotenv
load_dotenv()

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from src.simulations.workers.ruble_eod.tasks import run_ruble_eod
from src.core.utils.console_logger import log_info

async def main():
    log_info("Ruble EOD Worker", "Инициализация воркера смарт-контрактов...")
    
    scheduler = AsyncIOScheduler()
    
    # Запуск по расписанию в 23:15, согласно утвержденному плану
    scheduler.add_job(run_ruble_eod, 'cron', hour=23, minute=15)
    
    scheduler.start()
    log_info("Ruble EOD Worker", "Воркер цифрового рубля запущен. Ожидание 23:15...")
    
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log_info("Ruble EOD Worker", "Воркер остановлен.")
