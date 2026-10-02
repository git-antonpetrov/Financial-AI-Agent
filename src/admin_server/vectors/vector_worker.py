import os
import re
import json
import time
import datetime
import yaml
from minio import Minio
# pyrefly: ignore [missing-import]
import redis
import chromadb
import litellm
from langchain_text_splitters import RecursiveCharacterTextSplitter
from core.utils.console_logger import log_info, log_success, log_warning, log_error
import psycopg2
from psycopg2.pool import SimpleConnectionPool

# --- Настройки окружения ---
# Redis
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")
QUEUE_NAME = "document_tasks"
PROCESSING_QUEUE_NAME = "document_tasks_processing"

# MinIO
MINIO_URL = os.getenv("MINIO_URL", "minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")

# ChromaDB
CHROMA_HOST = os.getenv("CHROMA_HOST", "chromadb")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8000"))
CHROMA_AUTH_TOKEN = os.getenv("CHROMA_AUTH_TOKEN", "")

# Google Vertex AI (через прокси)
VERTEX_BASE_URL = os.getenv("VERTEX_BASE_URL", "").rstrip('/')

def get_vertex_api_base(model_name: str) -> str | None:
    if not VERTEX_BASE_URL:
        return None
    clean_model = model_name.replace("vertex_ai/", "")
    return f"{VERTEX_BASE_URL}/v1/projects/{VERTEX_PROJECT}/locations/{VERTEX_LOCATION}/publishers/google/models/{clean_model}"

VERTEX_PROJECT = os.getenv("VERTEX_PROJECT", "financial-ai-agent-0")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "global")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL_NAME", "gemini-embedding-2")

# База данных
DB_HOST = os.getenv("DB_HOST", "postgres-db")
POSTGRES_USER = os.getenv("POSTGRES_USER", "financial_ai_agent")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD")
if not POSTGRES_PASSWORD:
    log_error("Worker Init", "CRITICAL CONFIGURATION ERROR: POSTGRES_PASSWORD environment variable is required but not set.")
    exit(1)
POSTGRES_DB = os.getenv("POSTGRES_DB", "financial_agent")

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
    decode_responses=True
)

minio_client = Minio(
    MINIO_URL,
    access_key=MINIO_ACCESS_KEY,
    secret_key=MINIO_SECRET_KEY,
    secure=False
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
log_info("Worker Init", "Ожидание ChromaDB...")
for attempt in range(30):
    try:
        chroma_client.heartbeat()
        log_success("Worker Init", "ChromaDB доступна.")
        break
    except Exception:
        time.sleep(2)
else:
    log_error("Worker Init", "ChromaDB не отвечает после 30 попыток. Завершение.")
    exit(1)

try:
    db_pool = SimpleConnectionPool(
        1, 10,
        host=DB_HOST,
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
        dbname=POSTGRES_DB
    )
except Exception as e:
    log_error("Worker Init", f"Failed to initialize database pool: {e}")
    exit(1)

def update_document_status(file_hash: str, agent_name: str, new_status: str, message: str = ""):
    """Обновляет статус документа в Postgres через пул соединений"""
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
        log_info("DB Update", f"Document status for {file_hash} ({agent_name}) set to {new_status}")
    except Exception as e:
        log_error("DB Update", f"Failed to update status in DB: {e}")
        if 'conn' in locals():
            db_pool.putconn(conn, close=True)

def mark_document_repealed(short_name: str, agent_name: str, message: str = ""):
    """Переводит отмененный документ в статус 'repealed' в таблице documents Postgres"""
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
            log_info("DB Update", f"Документ '{short_name}' ({agent_name}) помечен как 'repealed' (обновлено записей: {updated_rows})")
    except Exception as e:
        log_error("DB Update", f"Ошибка обновления статуса 'repealed' для {short_name}: {e}")
        if 'conn' in locals():
            db_pool.putconn(conn, close=True)

def get_embeddings_google(texts: list[str]) -> list[list[float]]:
    """
    Отправляет батч текстов к Vertex AI (через наш прокси) для получения эмбеддингов.
    Использует библиотеку litellm. Делаем по одному, чтобы избежать ошибки размерности (1 к 10).
    """
    embeddings = []
    for text in texts:
        response = litellm.embedding(
            model=EMBEDDING_MODEL,
            input=[text],
            api_base=get_vertex_api_base(EMBEDDING_MODEL),
            vertex_project=VERTEX_PROJECT if VERTEX_PROJECT else None,
            vertex_location=VERTEX_LOCATION if VERTEX_LOCATION else None
        )
        embeddings.append(response.data[0]["embedding"])
    return embeddings

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
    agent = task.get("agent")
    action = task.get("action")
    file_path = task.get("file_path")
    bucket = task.get("bucket")
    file_hash = task.get("file_hash")
    
    log_info("Task Processing", f"Новая задача: [{action}] Агент: {agent}, Файл: {file_path}")
    
    try:
        # 1. Скачиваем файл из MinIO
        try:
            response = minio_client.get_object(bucket, file_path)
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
            
            log_success("Task Processing", f"Задача delete выполнена. Обработано имен для удаления: {deleted_count}")
            update_document_status(file_hash, agent, "completed", f"Успешно удалено {deleted_count} документов")
            
        elif action == "upsert":
            # 2. Парсинг и санитизация метаданных для ChromaDB
            raw_metadata = extract_metadata_from_markdown(markdown_text)
            short_name = raw_metadata.get("short_name")
            
            if not short_name:
                log_error("Metadata", "Не удалось найти 'short_name' в метаданных (YAML frontmatter) документа.")
                update_document_status(file_hash, agent, "error", "Отсутствует short_name в метаданных")
                return
                
            short_name = str(short_name).strip()
            log_info("Metadata", f"Документ распознан. short_name: '{short_name}'")
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
            log_info("Text Splitter", f"Текст разбит на {len(chunks)} чанков.")
            
            # 5. Батчинг и векторизация с откатом при ошибках
            try:
                for i in range(0, len(chunks), CHUNK_BATCH_SIZE):
                    batch_chunks = chunks[i:i + CHUNK_BATCH_SIZE]
                    log_info("Batching", f"Обработка батча {i//CHUNK_BATCH_SIZE + 1} (чанки {i+1} - {i+len(batch_chunks)})...")
                    
                    embeddings = get_embeddings_google(batch_chunks)
                    ids = [f"{short_name}_chunk_{i+j}" for j in range(len(batch_chunks))]
                    metadatas = [metadata.copy() for _ in batch_chunks]
                    
                    collection.add(
                        ids=ids,
                        embeddings=embeddings,
                        metadatas=metadatas,
                        documents=batch_chunks
                    )
                    
                    if i + CHUNK_BATCH_SIZE < len(chunks):
                        log_info("Rate Limit", f"Ждем {CHUNK_SLEEP_SECONDS} сек. для сброса лимитов Google...")
                        time.sleep(CHUNK_SLEEP_SECONDS)
            except Exception as e:
                log_error("Vectorization", f"Ошибка векторизации для '{short_name}': {e}")
                # Транзакционный откат: удаляем все добавленные частичные вектора данного документа
                try:
                    collection.delete(where={"short_name": short_name})
                    log_warning("ChromaDB Rollback", f"Откат: удалены частичные вектора для short_name='{short_name}'")
                except Exception as rollback_err:
                    log_error("ChromaDB Rollback", f"Ошибка при откате векторов: {rollback_err}")
                update_document_status(file_hash, agent, "error", f"Vectorization error: {e}")
                return

            log_success("ChromaDB", f"Успешно записано {len(chunks)} векторов для '{short_name}' в {collection_name}")
            update_document_status(file_hash, agent, "completed", "Успешно обработано воркером")
        else:
            log_error("Task Processing", f"Неизвестное действие: '{action}'")
            update_document_status(file_hash, agent, "error", f"Unknown action: {action}")
    finally:
        # Гарантированная уборка исходного файла из MinIO при любом сценарии
        if bucket and file_path:
            try:
                minio_client.remove_object(bucket, file_path)
                log_info("MinIO", f"Оригинальный файл {file_path} удален из MinIO.")
            except Exception as e:
                log_warning("MinIO", f"Предупреждение при удалении из MinIO {file_path}: {e}")

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
            log_warning("Recovery", f"Восстановлено {recovered} незавершенных задач из {PROCESSING_QUEUE_NAME} обратно в {QUEUE_NAME}")
    except Exception as e:
        log_error("Recovery", f"Ошибка при восстановлении незавершенных задач: {e}")

def fetch_task_reliable(timeout: int = 5) -> str | None:
    """
    Атомарно перемещает задачу из очереди задач в очередь обработки (Reliable Queue pattern).
    """
    try:
        # blmove доступен в Redis 6.2+
        return redis_client.blmove(QUEUE_NAME, PROCESSING_QUEUE_NAME, timeout=timeout, where_from="RIGHT", where_to="LEFT")
    except Exception:
        # Fallback на brpoplpush для совместимости
        return redis_client.brpoplpush(QUEUE_NAME, PROCESSING_QUEUE_NAME, timeout=timeout)

def main():
    log_success("Worker Start", f"Воркер запущен и слушает Redis ({QUEUE_NAME})...")
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
                log_error("Worker Error", f"Некорректный JSON задачи: {e}")
            except Exception as e:
                log_error("Worker Error", f"Необработанная ошибка при выполнении задачи: {e}")
                if 'task' in locals() and isinstance(task, dict):
                    update_document_status(task.get("file_hash"), task.get("agent"), "error", f"Unhandled worker error: {e}")
            finally:
                # Удаляем задачу из очереди обработки только после завершения process_task
                try:
                    redis_client.lrem(PROCESSING_QUEUE_NAME, 1, message_json)
                except Exception as e:
                    log_error("Redis", f"Не удалось удалить задачу из {PROCESSING_QUEUE_NAME}: {e}")
        except Exception as e:
            log_error("Worker Error", f"Ошибка в главном цикле воркера: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
