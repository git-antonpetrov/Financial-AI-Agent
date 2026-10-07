import os
import sys
import re
import json
import time
import datetime
import yaml
from minio import Minio
# pyrefly: ignore [missing-import]
import redis
import chromadb
try:
    from src.common.logger import log_info, log_success, log_warning, log_error
    from src.common.llm import default_llm_client, LLMClient
except ImportError:
    from common.logger import log_info, log_success, log_warning, log_error
    from common.llm import default_llm_client, LLMClient
from langchain_text_splitters import RecursiveCharacterTextSplitter
import psycopg2
from psycopg2.pool import SimpleConnectionPool
try:
    from chroma_envelope import (
        is_envelope_encryption_enabled,
        encrypt_batch,
        decrypt_chroma_results,
        encrypt_chroma_chunk,
        decrypt_chroma_chunk,
        encrypt_chroma_metadata,
        decrypt_chroma_metadata,
    )
except ImportError:
    try:
        from .chroma_envelope import (
            is_envelope_encryption_enabled,
            encrypt_batch,
            decrypt_chroma_results,
            encrypt_chroma_chunk,
            decrypt_chroma_chunk,
            encrypt_chroma_metadata,
            decrypt_chroma_metadata,
        )
    except ImportError:
        from src.admin_server.vectors.chroma_envelope import (
            is_envelope_encryption_enabled,
            encrypt_batch,
            decrypt_chroma_results,
            encrypt_chroma_chunk,
            decrypt_chroma_chunk,
            encrypt_chroma_metadata,
            decrypt_chroma_metadata,
        )

# --- Настройки окружения ---
# Redis
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")
REDIS_SSL = os.getenv("REDIS_SSL", "false").lower() in ("true", "1", "yes")
QUEUE_NAME = "document_tasks"
PROCESSING_QUEUE_NAME = "document_tasks_processing"

# MinIO
MINIO_URL = os.getenv("MINIO_URL", "minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() in ("true", "1", "yes")
MINIO_SSE_C_KEY = os.getenv("MINIO_SSE_C_KEY", "")


def get_minio_ssec():
    """
    Возвращает объект ServerSideEncryptionCustomerKey (SSE-C) для MinIO, если настроен ключ шифрования.
    При использовании SSE-S3 MinIO расшифровывает объекты автоматически на стороне сервера.
    """
    key_source = MINIO_SSE_C_KEY or os.getenv("MINIO_SSE_C_KEY", "")
    if not key_source:
        return None
    try:
        from minio.sse import SseCustomerKey
        key_raw = key_source.strip()
        if len(key_raw) == 64:
            try:
                key_bytes = bytes.fromhex(key_raw)
            except ValueError:
                key_bytes = key_raw.encode("utf-8")
        else:
            try:
                import base64
                key_bytes = base64.b64decode(key_raw)
                if len(key_bytes) != 32:
                    key_bytes = key_raw.encode("utf-8")
            except Exception:
                key_bytes = key_raw.encode("utf-8")
        if len(key_bytes) != 32:
            import hashlib
            key_bytes = hashlib.sha256(key_bytes).digest()
        return SseCustomerKey(key_bytes)
    except Exception as e:
        log_warning("MinIO SSE", f"Не удалось инициализировать SSE-C ключ: {e}")
        return None

# ChromaDB
CHROMA_HOST = os.getenv("CHROMA_HOST", "chromadb")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8000"))
CHROMA_AUTH_TOKEN = os.getenv("CHROMA_AUTH_TOKEN", "")

# Google Vertex AI (через прокси)
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL_NAME", "gemini-embedding-2")

def get_vertex_api_base(model_name: str) -> str | None:
    return default_llm_client.get_vertex_api_base(model_name)

# База данных
DB_HOST = os.getenv("DB_HOST", "postgres-db")
POSTGRES_USER = os.getenv("POSTGRES_USER", "financial_ai_agent")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD")
if not POSTGRES_PASSWORD:
    log_error("Инициализация воркера", "Критическая ошибка конфигурации: переменная окружения POSTGRES_PASSWORD обязательна, но не задана.")
    sys.exit(1)
POSTGRES_DB = os.getenv("POSTGRES_DB", "financial_agent")
POSTGRES_SSLMODE = os.getenv("POSTGRES_SSLMODE", "").strip()

# Лимиты и Чанкинг
CHUNK_BATCH_SIZE = int(os.getenv("CHUNK_BATCH_SIZE", "10"))
CHUNK_SLEEP_SECONDS = int(os.getenv("CHUNK_SLEEP_SECONDS", "10"))
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1000"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "100"))

# --- Инициализация клиентов ---
log_info("Worker Init", "Инициализация клиентов...")

redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    password=REDIS_PASSWORD,
    ssl=REDIS_SSL,
    decode_responses=True
)

minio_client = Minio(
    MINIO_URL,
    access_key=MINIO_ACCESS_KEY,
    secret_key=MINIO_SECRET_KEY,
    secure=MINIO_SECURE
)

# Подключаемся к запущенному контейнеру Chroma
chroma_client = chromadb.HttpClient(
    host=CHROMA_HOST, 
    port=CHROMA_PORT,
    settings=chromadb.config.Settings(
        chroma_client_auth_provider="chromadb.auth.token.TokenAuthClientProvider",
        chroma_client_auth_credentials=CHROMA_AUTH_TOKEN
    )
)

# Ожидание готовности ChromaDB
log_info("Инициализация воркера", "Ожидание ChromaDB...")
for attempt in range(30):
    try:
        chroma_client.heartbeat()
        log_success("Инициализация воркера", "ChromaDB доступна.")
        break
    except Exception:
        time.sleep(2)
else:
    log_error("Инициализация воркера", "ChromaDB не отвечает после 30 попыток. Завершение работы.")
    sys.exit(1)

try:
    pool_kwargs = {
        "minconn": 1,
        "maxconn": 10,
        "host": DB_HOST,
        "user": POSTGRES_USER,
        "password": POSTGRES_PASSWORD,
        "dbname": POSTGRES_DB,
    }
    if POSTGRES_SSLMODE:
        pool_kwargs["sslmode"] = POSTGRES_SSLMODE
    db_pool = SimpleConnectionPool(**pool_kwargs)
except Exception as e:
    log_error("Инициализация воркера", f"Не удалось инициализировать пул соединений с базой данных: {e}")
    sys.exit(1)

def update_document_status(file_hash: str, agent_name: str, new_status: str, message: str = ""):
    """Обновляет статус документа в Postgres через пул соединений."""
    if not file_hash or not agent_name:
        return
    try:
        conn = db_pool.getconn()
        cur = conn.cursor()
        cur.execute(
            "UPDATE documents SET status = %s, message = %s WHERE file_hash = %s AND agent_name = %s AND status IN ('processing', 'checking')",
            (new_status, message, file_hash, agent_name)
        )
        conn.commit()
        cur.close()
        db_pool.putconn(conn)
        log_info("Обновление БД", f"Статус документа {file_hash} ({agent_name}) изменен на '{new_status}'")
    except Exception as e:
        log_error("Обновление БД", f"Не удалось обновить статус документа в БД: {e}")
        if 'conn' in locals():
            db_pool.putconn(conn, close=True)

def mark_document_repealed(short_name: str, agent_name: str, message: str = ""):
    """Переводит отмененный документ в статус 'repealed' в таблице documents Postgres."""
    if not short_name or not agent_name:
        return
    try:
        conn = db_pool.getconn()
        cur = conn.cursor()
        cur.execute(
            "UPDATE documents SET status = 'repealed', message = %s WHERE short_name = %s AND agent_name = %s AND status != 'repealed'",
            (message, short_name, agent_name)
        )
        updated_rows = cur.rowcount
        conn.commit()
        cur.close()
        db_pool.putconn(conn)
        if updated_rows > 0:
            log_info("Обновление БД", f"Документ '{short_name}' ({agent_name}) помечен как 'repealed' (обновлено записей: {updated_rows})")
    except Exception as e:
        log_error("Обновление БД", f"Ошибка обновления статуса 'repealed' для {short_name}: {e}")
        if 'conn' in locals():
            db_pool.putconn(conn, close=True)

def get_embeddings_google(texts: list[str]) -> list[list[float]]:
    """
    Отправляет батч текстов к Vertex AI (через прокси) для получения эмбеддингов.
    Использует централизованный LLMClient с автоматическими повторными попытками при ошибках 429 (Resource Exhausted).
    """
    return default_llm_client.get_embeddings_batch(
        model=EMBEDDING_MODEL,
        texts=texts
    )

def extract_metadata_from_markdown(markdown_text: str) -> dict:
    """
    Безопасно извлекает метаданные из YAML frontmatter в начале Markdown файла
    с использованием yaml.safe_load.
    """
    if not markdown_text:
        return {}
        
    text = markdown_text.lstrip('\ufeff')
    # Ищем блок между первым --- и закрывающим ---
    match = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)", text, flags=re.DOTALL)
    if not match:
        return {}
        
    frontmatter_raw = match.group(1)
    try:
        data = yaml.safe_load(frontmatter_raw)
        if isinstance(data, dict):
            return data
        return {}
    except Exception as e:
        log_error("YAML", f"Ошибка безопасного парсинга frontmatter: {e}")
        return {}

def sanitize_chroma_metadata(metadata: dict) -> dict:
    """
    Санитизирует словарь метаданных для ChromaDB.
    ChromaDB поддерживает только базовые скалярные типы: str, int, float, bool.
    Значения None удаляются, сложные структуры (list, dict) сериализуются в JSON-строки,
    даты конвертируются в ISO-строки.
    """
    if not isinstance(metadata, dict):
        return {}
        
    sanitized = {}
    for k, v in metadata.items():
        if v is None:
            continue
        key_str = str(k)
        if isinstance(v, bool):
            sanitized[key_str] = v
        elif isinstance(v, (int, float, str)):
            sanitized[key_str] = v
        elif isinstance(v, (datetime.date, datetime.datetime)):
            sanitized[key_str] = v.isoformat()
        elif isinstance(v, (list, tuple, dict)):
            sanitized[key_str] = json.dumps(v, ensure_ascii=False)
        else:
            sanitized[key_str] = str(v)
            
    return sanitized

def process_task(task: dict):
    """
    Обрабатывает задачу векторизации или удаления документа из очереди.
    Загружает файл из MinIO, генерирует эмбеддинги, обновляет ChromaDB и статус в PostgreSQL.
    """
    agent = task.get("agent")
    action = task.get("action")
    file_path = task.get("file_path")
    bucket = task.get("bucket")
    file_hash = task.get("file_hash")
    task_success = False
    
    log_info("Обработка задачи", f"Новая задача: [{action}] Агент: {agent}, Файл: {file_path}")
    
    try:
        # 1. Скачиваем файл из MinIO
        try:
            get_kwargs = {}
            ssec_obj = get_minio_ssec()
            if ssec_obj is not None:
                get_kwargs["ssec"] = ssec_obj
            response = minio_client.get_object(bucket, file_path, **get_kwargs)
            markdown_text = response.read().decode('utf-8')
        except Exception as e:
            log_error("MinIO", f"Ошибка скачивания файла {file_path} из MinIO: {e}")
            update_document_status(file_hash, agent, "error", f"MinIO error: {e}")
            return
        finally:
            if 'response' in locals():
                response.close()
                
        # Получаем/создаем коллекцию для конкретного агента
        collection_name = f"knowledge-{agent}"
        collection = chroma_client.get_or_create_collection(name=collection_name)
        
        if action == "delete":
            # Для action=delete ожидаем файл, где каждая строка - это short_name документа для удаления
            lines = markdown_text.split("\n")
            deleted_count = 0
            for line in lines:
                target_short_name = line.strip()
                if target_short_name:
                    try:
                        collection.delete(where={"short_name": target_short_name})
                        log_info("ChromaDB", f"Удалены вектора с short_name='{target_short_name}' из коллекции {collection_name}")
                        deleted_count += 1
                        mark_document_repealed(
                            short_name=target_short_name,
                            agent_name=agent,
                            message=f"Документ отменен и удален из векторного индекса ({collection_name})"
                        )
                    except Exception as e:
                        log_warning("ChromaDB", f"Предупреждение при удалении '{target_short_name}': {e}")
            
            log_success("Обработка задачи", f"Задача delete выполнена. Обработано имен для удаления: {deleted_count}")
            update_document_status(file_hash, agent, "completed", f"Успешно удалено {deleted_count} документов")
            task_success = True
            
        elif action == "upsert":
            # 2. Парсинг и санитизация метаданных для ChromaDB
            raw_metadata = extract_metadata_from_markdown(markdown_text)
            short_name = raw_metadata.get("short_name")
            
            if not short_name:
                log_error("Метаданные", "Не удалось найти 'short_name' в метаданных (YAML frontmatter) документа.")
                update_document_status(file_hash, agent, "error", "Отсутствует short_name в метаданных")
                return
                
            short_name = str(short_name).strip()
            log_info("Метаданные", f"Документ распознан. short_name: '{short_name}'")
            metadata = sanitize_chroma_metadata(raw_metadata)
            metadata["short_name"] = short_name
            
            # 3. Очистка старых векторов перед записью новых
            try:
                collection.delete(where={"short_name": short_name})
                log_info("ChromaDB", f"Удалены старые записи с short_name='{short_name}' из коллекции {collection_name} перед upsert")
            except Exception as e:
                log_warning("ChromaDB", f"Предупреждение при удалении старых векторов: {e}")

            # 4. Нарезка на чанки
            clean_text = markdown_text
            if clean_text.startswith("---"):
                parts = clean_text.split("---", 2)
                if len(parts) >= 3:
                    clean_text = parts[2].strip()

            splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
            chunks = splitter.split_text(clean_text)
            log_info("Разбивка текста", f"Текст разбит на {len(chunks)} чанков.")
            
            # 5. Батчинг и векторизация с откатом при ошибках
            try:
                for i in range(0, len(chunks), CHUNK_BATCH_SIZE):
                    batch_chunks = chunks[i:i + CHUNK_BATCH_SIZE]
                    log_info("Батчинг", f"Обработка батча {i//CHUNK_BATCH_SIZE + 1} (чанки {i+1} - {i+len(batch_chunks)})...")
                    
                    embeddings = get_embeddings_google(batch_chunks)
                    ids = [f"{short_name}_chunk_{i+j}" for j in range(len(batch_chunks))]
                    metadatas = [metadata.copy() for _ in batch_chunks]
                    
                    if is_envelope_encryption_enabled():
                        docs_to_store, metadatas_to_store = encrypt_batch(batch_chunks, metadatas)
                    else:
                        docs_to_store, metadatas_to_store = batch_chunks, metadatas

                    collection.add(
                        ids=ids,
                        embeddings=embeddings,
                        metadatas=metadatas_to_store,
                        documents=docs_to_store
                    )
                    
                    if i + CHUNK_BATCH_SIZE < len(chunks):
                        log_info("Лимит запросов", f"Ожидание {CHUNK_SLEEP_SECONDS} сек. для сброса лимитов Google...")
                        time.sleep(CHUNK_SLEEP_SECONDS)
            except Exception as e:
                log_error("Векторизация", f"Ошибка векторизации для '{short_name}': {e}")
                # Транзакционный откат: удаляем все добавленные частичные вектора данного документа
                try:
                    collection.delete(where={"short_name": short_name})
                    log_warning("Откат ChromaDB", f"Откат: удалены частичные вектора для short_name='{short_name}'")
                except Exception as rollback_err:
                    log_error("Откат ChromaDB", f"Ошибка при откате векторов: {rollback_err}")
                update_document_status(file_hash, agent, "error", f"Vectorization error: {e}")
                return

            log_success("ChromaDB", f"Успешно записано {len(chunks)} векторов для '{short_name}' в {collection_name}")
            update_document_status(file_hash, agent, "completed", "Успешно обработано воркером")
            task_success = True
        else:
            log_error("Обработка задачи", f"Неизвестное действие: '{action}'")
            update_document_status(file_hash, agent, "error", f"Unknown action: {action}")

        # Удаление файла из MinIO выполняется ТОЛЬКО после успешной векторизации и фиксации статуса
        if task_success and bucket and file_path:
            try:
                minio_client.remove_object(bucket, file_path)
                log_info("MinIO", f"Оригинальный файл {file_path} удален из MinIO после успешной обработки.")
            except Exception as e:
                log_warning("MinIO", f"Предупреждение при удалении из MinIO {file_path}: {e}")
    finally:
        # Если задача завершилась с ошибкой, файл сохраняется в MinIO для возможности восстановления и повторной обработки
        if not task_success and file_path:
            log_warning("MinIO", f"Исходный файл {file_path} сохранен в MinIO для возможности повторной обработки.")

def recover_abandoned_tasks():
    """
    При старте воркера возвращает задачи, оставшиеся в очереди обработки
    после неожиданного перезапуска или сбоя воркера, обратно в основную очередь.
    """
    recovered = 0
    try:
        while True:
            # Атомарно перемещаем хвост очереди обработки в голову основной очереди
            task_data = redis_client.rpoplpush(PROCESSING_QUEUE_NAME, QUEUE_NAME)
            if not task_data:
                break
            recovered += 1
        if recovered > 0:
            log_warning("Восстановление", f"Восстановлено {recovered} незавершенных задач из {PROCESSING_QUEUE_NAME} обратно в {QUEUE_NAME}")
    except Exception as e:
        log_error("Восстановление", f"Ошибка при восстановлении незавершенных задач: {e}")

def fetch_task_reliable(timeout: int = 5) -> str | None:
    """
    Атомарно перемещает задачу из очереди задач в очередь обработки (паттерн надежной очереди).
    """
    try:
        # Метод blmove доступен в Redis 6.2+
        return redis_client.blmove(QUEUE_NAME, PROCESSING_QUEUE_NAME, timeout=timeout, where_from="RIGHT", where_to="LEFT")
    except Exception:
        # Резервный вызов brpoplpush для совместимости
        return redis_client.brpoplpush(QUEUE_NAME, PROCESSING_QUEUE_NAME, timeout=timeout)

def main():
    """Запускает основной цикл воркера обработки документов."""
    log_success("Запуск воркера", f"Воркер запущен и слушает Redis ({QUEUE_NAME})...")
    recover_abandoned_tasks()

    while True:
        try:
            # Атомарное получение задачи с помещением в PROCESSING_QUEUE_NAME
            message_json = fetch_task_reliable(timeout=5)
            if not message_json:
                continue

            try:
                task = json.loads(message_json)
                process_task(task)
            except json.JSONDecodeError as e:
                log_error("Ошибка воркера", f"Некорректный JSON задачи: {e}")
            except Exception as e:
                log_error("Ошибка воркера", f"Необработанная ошибка при выполнении задачи: {e}")
                if 'task' in locals() and isinstance(task, dict):
                    update_document_status(task.get("file_hash"), task.get("agent"), "error", f"Unhandled worker error: {e}")
            finally:
                # Удаляем задачу из очереди обработки только после завершения process_task
                try:
                    redis_client.lrem(PROCESSING_QUEUE_NAME, 1, message_json)
                except Exception as e:
                    log_error("Redis", f"Не удалось удалить задачу из {PROCESSING_QUEUE_NAME}: {e}")
        except Exception as e:
            log_error("Ошибка воркера", f"Ошибка в главном цикле воркера: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
