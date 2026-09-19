import os
import sys
import asyncio

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from dotenv import load_dotenv
load_dotenv()

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from src.simulations.workers.invest_eod.tasks import run_invest_eod
from src.core.utils.console_logger import log_info

async def main():
    log_info("Invest EOD Worker", "Инициализация воркера...")
    
    # Создаем асинхронный планировщик
    scheduler = AsyncIOScheduler()
    
    # Запуск по расписанию в 23:15, как просил пользователь
    scheduler.add_job(run_invest_eod, 'cron', hour=23, minute=15)
    
    scheduler.start()
    log_info("Invest EOD Worker", "Воркер инвестиций запущен. Ожидание 23:15...")
    
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        # При запуске вручную можно раскомментировать вызов EOD для тестирования:
        # asyncio.run(run_invest_eod())
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log_info("Invest EOD Worker", "Воркер остановлен.")
