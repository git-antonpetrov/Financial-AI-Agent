import os
import sys
import json
import asyncio
import datetime
from filelock import FileLock, Timeout
from src.core.utils.console_logger import log_info, log_warning, log_error

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from src.elt.scripts.extract.parsers.cbr_rss_parser import run_cbr_parser
from src.elt.scripts.extract.parsers.moex_parser import run_moex_parser

from src.elt.clients.db import get_async_session_maker, init_db
from src.elt.clients.storage import get_minio_client
from src.core.clients.llm import get_cloud_ai_client, get_embedder
from src.core.clients.vector_db import get_chroma_client

try:
    # pyrefly: ignore [missing-import]
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
except ImportError:
    log_error("Оркестратор Парсеров", "Библиотека apscheduler не установлена. Установите ее: pip install apscheduler")
    sys.exit(1)

# MainPipeline импортируется для обработки загруженных данных
try:
    from src.elt.scripts.load.main_pipeline import MainPipeline
    has_main_pipeline = True
except ImportError:
    has_main_pipeline = False

async def _run_main_logic():
    """
    Основная логика запуска всех этапов обработки данных (ELT).
    Сначала параллельно запускает парсеры. При наличии новых файлов запускает
    главный конвейер загрузки (Load), а затем трансформации и векторизации (Transform).
    """
    log_info("Оркестратор Парсеров", "Инициализация...")
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    
    db_session_maker = get_async_session_maker()
    minio_client = get_minio_client()
    
    # Проверка даты последнего запуска
    today_date = datetime.date.today()
    
    try:
        raw_objects = list(minio_client.list_objects("raw-documents", recursive=True))
        rag_upsert_objects = list(minio_client.list_objects("rag-documents", prefix="upsert/", recursive=True))
        rag_delete_objects = list(minio_client.list_objects("rag-documents", prefix="delete/", recursive=True))
        
        raw_count = len(raw_objects)
        upsert_count = len([obj for obj in rag_upsert_objects if not obj.object_name.endswith("/")])
        delete_count = len([obj for obj in rag_delete_objects if not obj.object_name.endswith("/")])
        
        has_pending_files = (raw_count > 0) or (upsert_count > 0) or (delete_count > 0)
    except Exception:
        has_pending_files = False

    try:
        from src.elt.db.models import OrchestratorRun
        from sqlalchemy import select
        
        async with db_session_maker() as session:
            result = await session.execute(select(OrchestratorRun).where(OrchestratorRun.run_date == today_date))
            existing_run = result.scalar_one_or_none()
            if existing_run and not has_pending_files:
                log_warning("Оркестратор Парсеров", f"Скрипт уже запускался сегодня ({today_date}) и очередь файлов пуста. Пропуск.")
                return
    except Exception as e:
        log_error("Оркестратор Парсеров", f"Ошибка проверки состояния в БД: {e}")
        return
            
    log_info("Оркестратор Парсеров", f"Запуск параллельного обновления парсеров (дата: {today_date})...")
    started_at = datetime.datetime.now()
    
    # Парсеры запускаются параллельно в одном asyncio цикле
    moex_task = run_moex_parser(db_session_maker, minio_client, base_dir)
    cbr_task = run_cbr_parser(db_session_maker, minio_client, base_dir)
    
    results = await asyncio.gather(moex_task, cbr_task)
    
    moex_has_new = results[0]
    cbr_has_new = results[1]
    has_new_files = moex_has_new or cbr_has_new
    
    # Трекер обновляется после успешного запуска
    try:
        async with db_session_maker() as session:
            new_run = OrchestratorRun(
                run_date=today_date,
                started_at=started_at,
                completed_at=datetime.datetime.now(),
                has_new_files=has_new_files
            )
            session.add(new_run)
            await session.commit()
    except Exception as e:
        log_error("Оркестратор Парсеров", f"Ошибка сохранения состояния запуска в БД: {e}")
    
    # Проверка наличия недообработанных файлов (в случае падения пайплайна ранее)
    try:
        raw_objects = list(minio_client.list_objects("raw-documents", recursive=True))
        
        # Проверяем также наличие файлов, застрявших на этапе Transform
        rag_upsert_objects = list(minio_client.list_objects("rag-documents", prefix="upsert/", recursive=True))
        rag_delete_objects = list(minio_client.list_objects("rag-documents", prefix="delete/", recursive=True))
        
        raw_count = len(raw_objects)
        upsert_count = len([obj for obj in rag_upsert_objects if not obj.object_name.endswith("/")])
        delete_count = len([obj for obj in rag_delete_objects if not obj.object_name.endswith("/")])
        
        has_pending_files = (raw_count > 0) or (upsert_count > 0) or (delete_count > 0)
    except Exception:
        has_pending_files = False

    if moex_has_new or cbr_has_new or has_pending_files:
        log_info("Оркестратор Парсеров", "Есть файлы для обработки. Начинается цепочка обработки.")
        
        cloud_ai = get_cloud_ai_client()
        embedder = get_embedder()

        # 1. Этап Load: MainPipeline
        if has_main_pipeline:
            log_info("Оркестратор Парсеров", "Пауза 30 секунд перед запуском MainPipeline...")
            await asyncio.sleep(30)
            
            log_info("MainPipeline", "Запуск MainPipeline (Load Layer)...")
            pipeline = MainPipeline(db_session_maker, minio_client, cloud_ai)
            try:
                await pipeline.run()
                log_info("MainPipeline", "MainPipeline успешно завершил работу.")
            except Exception as e:
                log_error("MainPipeline", f"Ошибка при выполнении MainPipeline (этап Load): {e}")
                return # Если произошел сбой на этапе Load, обработка останавливается
                
        # 2. Этап Transform: ChromaDB Delete
        try:
            from src.elt.scripts.transform.chromadb_delete import ChromaDBDelete
            log_info("Оркестратор Парсеров", "Пауза 30 секунд перед запуском ChromaDBDelete...")
            await asyncio.sleep(30)
            
            chroma_client = get_chroma_client()
            log_info("ChromaDB Delete", "Запуск ChromaDBDelete (удаление старых векторов)...")
            del_pipeline = ChromaDBDelete(db_session_maker, minio_client, chroma_client, embedder)
            await del_pipeline.run()
            log_info("ChromaDB Delete", "ChromaDBDelete успешно завершил работу.")
        except Exception as e:
            import traceback
            log_error("ChromaDB Delete", f"Ошибка при выполнении ChromaDBDelete:\n{traceback.format_exc()}")
            return # Если произошел сбой на этапе Delete, Upsert запускать нельзя во избежание дублей
            
        # 3. Этап Transform: ChromaDB Upsert
        try:
            from src.elt.scripts.transform.chromadb_upsert import ChromaDBUpsert
            log_info("Оркестратор Парсеров", "Пауза 30 секунд перед запуском ChromaDBUpsert...")
            await asyncio.sleep(30)
            
            chroma_client = get_chroma_client()
            log_info("ChromaDB Upsert", "Запуск ChromaDBUpsert (загрузка новых векторов)...")
            upsert_pipeline = ChromaDBUpsert(db_session_maker, minio_client, chroma_client, embedder)
            await upsert_pipeline.run()
            log_info("ChromaDB Upsert", "ChromaDBUpsert успешно завершил работу.")
        except Exception as e:
            log_error("ChromaDB Upsert", f"Ошибка при выполнении ChromaDBUpsert: {e}")
    else:
        log_info("Оркестратор Парсеров", "Нет новых файлов ни в одном из источников.")

async def main():
    """
    Обертка для запуска основной логики с использованием файловой блокировки.
    Предотвращает одновременный запуск нескольких пайплайнов.
    """
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    lock_file = os.path.join(base_dir, "orchestrator.lock")
    lock = FileLock(lock_file, timeout=0)
    
    try:
        with lock:
            await _run_main_logic()
    except Timeout:
        log_warning("Оркестратор Парсеров", "Пайплайн уже запущен в другом процессе. Пропуск.")
        return

async def start_service():
    """
    Запускает фоновый сервис планировщика APScheduler.
    Гарантирует выполнение пайплайна сразу при запуске и далее раз в сутки по расписанию.
    """
    await init_db()

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
