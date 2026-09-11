import os
import sys
import json
import asyncio
import datetime
from filelock import Timeout
from src.utils.console_logger import log_info, log_warning, log_error

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from src.elt.extract.parsers.cbr_rss_parser import run_cbr_parser
from src.elt.extract.parsers.moex_parser import run_moex_parser

try:
    # pyrefly: ignore [missing-import]
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
except ImportError:
    log_error("Оркестратор Парсеров", "Библиотека apscheduler не установлена. Установите ее: pip install apscheduler")
    sys.exit(1)

# MainPipeline импортируется для обработки загруженных данных
try:
    from src.elt.load.main_pipeline import MainPipeline
    has_main_pipeline = True
except ImportError:
    has_main_pipeline = False

async def main():
    """
    Основная логика запуска всех этапов обработки данных (ELT).
    Сначала параллельно запускает парсеры. При наличии новых файлов запускает
    главный конвейер загрузки (Load), а затем трансформации и векторизации (Transform).
    """
    log_info("Оркестратор Парсеров", "Инициализация...")
    
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
                    log_warning("Оркестратор Парсеров", f"Скрипт уже запускался сегодня ({today_str}). Пропуск.")
                    return
        except json.JSONDecodeError:
            pass
            
    log_info("Оркестратор Парсеров", f"Запуск параллельного обновления парсеров (дата: {today_str})...")
    
    # Парсеры запускаются параллельно через потоки, так как их точки входа используют asyncio.run()
    moex_task = asyncio.to_thread(run_moex_parser, base_dir)
    cbr_task = asyncio.to_thread(run_cbr_parser, base_dir)
    
    results = await asyncio.gather(moex_task, cbr_task)
    
    moex_has_new = results[0]
    cbr_has_new = results[1]
    
    # Трекер обновляется после успешного запуска
    with open(tracker_path, "w", encoding="utf-8") as f:
        json.dump({"last_run_date": today_str}, f, indent=4)
    
    if moex_has_new or cbr_has_new:
        log_info("Оркестратор Парсеров", "Обнаружены новые загруженные файлы. Начинается цепочка обработки.")
        
        # 1. Этап Load: MainPipeline
        if has_main_pipeline:
            log_info("MainPipeline", "Запуск MainPipeline (Load Layer)...")
            pipeline = MainPipeline()
            try:
                await pipeline.run()
                log_info("MainPipeline", "MainPipeline успешно завершил работу.")
            except Exception as e:
                log_error("MainPipeline", f"Ошибка при выполнении MainPipeline (этап Load): {e}")
                return # Если произошел сбой на этапе Load, обработка останавливается
                
        # 2. Этап Transform: ChromaDB Delete
        try:
            from src.elt.transform.chromadb_delete import ChromaDBDelete
            log_info("ChromaDB Delete", "Запуск ChromaDBDelete (удаление старых векторов)...")
            del_pipeline = ChromaDBDelete()
            await del_pipeline.run()
            log_info("ChromaDB Delete", "ChromaDBDelete успешно завершил работу.")
        except Exception as e:
            log_error("ChromaDB Delete", f"Ошибка при выполнении ChromaDBDelete: {e}")
            return # Если произошел сбой на этапе Delete, Upsert запускать нельзя во избежание дублей
            
        # 3. Этап Transform: ChromaDB Upsert
        try:
            from src.elt.transform.chromadb_upsert import ChromaDBUpsert
            log_info("ChromaDB Upsert", "Запуск ChromaDBUpsert (загрузка новых векторов)...")
            upsert_pipeline = ChromaDBUpsert()
            await upsert_pipeline.run()
            log_info("ChromaDB Upsert", "ChromaDBUpsert успешно завершил работу.")
        except Exception as e:
            log_error("ChromaDB Upsert", f"Ошибка при выполнении ChromaDBUpsert: {e}")
    else:
        log_info("Оркестратор Парсеров", "Нет новых файлов ни в одном из источников.")

async def start_service():
    """
    Запускает фоновый сервис планировщика APScheduler.
    Гарантирует выполнение пайплайна сразу при запуске и далее раз в сутки по расписанию.
    """
    log_info("Оркестратор Парсеров", "Запуск фонового сервиса (APScheduler)...")
    
    # Настраивается планировщик
    scheduler = AsyncIOScheduler()
    # Задача планируется на каждый день в 00:05
    scheduler.add_job(main, 'cron', hour=0, minute=5)
    scheduler.start()
    log_info("Оркестратор Парсеров", "Задача запланирована на ежедневное выполнение в 00:05.")
    
    # Обязательный первый прогон при самом запуске контейнера
    await main()
    
    # Процесс остается запущенным навсегда
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(start_service())
