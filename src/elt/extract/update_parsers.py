import os
import sys
import json
import asyncio
import datetime
from filelock import Timeout

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from src.elt.extract.parsers.cbr_rss_parser import run_cbr_parser
from src.elt.extract.parsers.moex_parser import run_moex_parser

try:
    # pyrefly: ignore [missing-import]
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
except ImportError:
    print("\033[91m[Ошибка]\033[0m Библиотека apscheduler не установлена. Установите ее: pip install apscheduler")
    sys.exit(1)

# StorageManager пока импортируем, но он будет переделан позже
try:
    from src.elt.load.storage_manager import StorageManager
    has_storage_manager = True
except ImportError:
    has_storage_manager = False

async def main():
    print("\033[96m[Оркестратор]\033[0m Инициализация...")
    
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    cache_dir = os.path.join(base_dir, "data", "cache")
    os.makedirs(cache_dir, exist_ok=True)
    
    tracker_path = os.path.join(cache_dir, "orchestrator_state.json")
    
    # Проверка даты последнего запуска
    today_str = datetime.date.today().isoformat()
    
    if os.path.exists(tracker_path):
        try:
            with open(tracker_path, "r", encoding="utf-8") as f:
                tracker = json.load(f)
                last_run = tracker.get("last_run_date")
                if last_run == today_str:
                    print(f"\033[93m[Оркестратор]\033[0m Скрипт уже запускался сегодня ({today_str}). Пропуск.")
                    return
        except json.JSONDecodeError:
            pass
            
    print(f"\033[96m[Оркестратор]\033[0m Запуск параллельного обновления парсеров (дата: {today_str})...")
    
    # Запускаем парсеры параллельно через потоки, так как их точки входа используют asyncio.run()
    moex_task = asyncio.to_thread(run_moex_parser, base_dir)
    cbr_task = asyncio.to_thread(run_cbr_parser, base_dir)
    
    results = await asyncio.gather(moex_task, cbr_task)
    
    moex_has_new = results[0]
    cbr_has_new = results[1]
    
    # Обновляем трекер после успешного запуска
    with open(tracker_path, "w", encoding="utf-8") as f:
        json.dump({"last_run_date": today_str}, f, indent=4)
    
    if moex_has_new or cbr_has_new:
        print("\033[92m[Оркестратор]\033[0m Обнаружены новые загруженные файлы.")
        if has_storage_manager:
            print("\033[96m[Оркестратор]\033[0m Запуск StorageManager...")
            sm = StorageManager()
            try:
                await sm.process_all_landing_files()
                print("\033[92m[Оркестратор]\033[0m Обработка завершена.")
            except Timeout:
                print("\033[93m[Внимание]\033[0m StorageManager уже запущен в другом процессе.\033[0m")
    else:
        print("\033[96m[Оркестратор]\033[0m Нет новых файлов ни в одном из источников.")

async def start_service():
    print("\033[96m[Оркестратор]\033[0m Запуск фонового сервиса (APScheduler)...")
    
    # Настраиваем планировщик
    scheduler = AsyncIOScheduler()
    # Планируем задачу на каждый день в 00:05
    scheduler.add_job(main, 'cron', hour=0, minute=5)
    scheduler.start()
    print("\033[96m[Оркестратор]\033[0m Задача запланирована на ежедневное выполнение в 00:05.")
    
    # Обязательно делаем первый прогон при самом запуске контейнера
    await main()
    
    # Оставляем процесс запущенным навсегда, без while True
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(start_service())
