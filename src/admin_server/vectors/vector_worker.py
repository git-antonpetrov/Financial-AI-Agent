import os
import json
import time
from minio import Minio
# pyrefly: ignore [missing-import]
import redis
import chromadb
import litellm
from langchain_text_splitters import RecursiveCharacterTextSplitter
from core.utils.console_logger import log_info, log_success, log_warning, log_error

# --- Настройки окружения ---
# Redis
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")

# MinIO
MINIO_URL = os.getenv("MINIO_URL", "minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")

# ChromaDB
CHROMA_HOST = os.getenv("CHROMA_HOST", "chromadb")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8000"))

# Google Vertex AI (через прокси)
VERTEX_BASE_URL = os.getenv("VERTEX_BASE_URL", "").rstrip('/')
VERTEX_PROJECT = os.getenv("VERTEX_PROJECT", "financial-ai-agent-0")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "global")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL_NAME", "gemini-embedding-2")

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
    port=CHROMA_PORT
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

def get_embeddings_google(texts: list[str]) -> list[list[float]]:
    """
    Отправляет батч текстов к Vertex AI (через наш прокси) для получения эмбеддингов.
    Использует библиотеку litellm.
    """
    # litellm ожидает префикс vertex_ai/ для моделей Vertex
    model_name = EMBEDDING_MODEL if EMBEDDING_MODEL.startswith("vertex_ai/") else f"vertex_ai/{EMBEDDING_MODEL}"

    response = litellm.embedding(
        model=model_name,
        input=texts,
        api_base=VERTEX_BASE_URL if VERTEX_BASE_URL else None,
        vertex_project=VERTEX_PROJECT if VERTEX_PROJECT else None,
        vertex_location=VERTEX_LOCATION if VERTEX_LOCATION else None
    )
    
    # LiteLLM возвращает стандартизированный ответ, аналогичный OpenAI
    embeddings = [item["embedding"] for item in response.data]
    return embeddings

def extract_metadata_from_markdown(markdown_text: str) -> dict:
    """
    Извлекает метаданные из начала Markdown файла (YAML frontmatter).
    Возвращает словарь метаданных. Не делает фоллбэк на имя файла.
    """
    meta = {}
    
    # Пытаемся найти YAML frontmatter
    lines = markdown_text.split('\n')
    if lines and lines[0].strip() == '---':
        end_idx = -1
        for i in range(1, len(lines)):
            if lines[i].strip() == '---':
                end_idx = i
                break
        
        if end_idx != -1:
            for line in lines[1:end_idx]:
                if ':' in line:
                    k, v = line.split(':', 1)
                    meta[k.strip()] = v.strip()
                    
    return meta

def process_task(task: dict):
    agent = task.get("agent")
    action = task.get("action")
    file_path = task.get("file_path")
    bucket = task.get("bucket")
    
    log_info("Task Processing", f"Новая задача: [{action}] Агент: {agent}, Файл: {file_path}")
    
    # 1. Скачиваем файл из MinIO
    try:
        response = minio_client.get_object(bucket, file_path)
        markdown_text = response.read().decode('utf-8')
    except Exception as e:
        log_error("MinIO", f"Ошибка скачивания файла {file_path} из MinIO: {e}")
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
                except Exception as e:
                    log_warning("ChromaDB", f"Предупреждение при удалении '{target_short_name}': {e}")
        
        log_success("Task Processing", f"Задача delete выполнена. Обработано имен для удаления: {deleted_count}")
        
    elif action == "upsert":
        # 2. Парсинг метаданных
        metadata = extract_metadata_from_markdown(markdown_text)
        short_name = metadata.get("short_name")
        
        if not short_name:
            log_error("Metadata", "Не удалось найти 'short_name' в метаданных (YAML frontmatter) документа.")
            # Файл всё равно удаляем из MinIO, чтобы он там не завис навсегда
            try:
                minio_client.remove_object(bucket, file_path)
            except:
                pass
            return
            
        log_info("Metadata", f"Документ распознан. short_name: '{short_name}'")
        
        # 3. Очистка (Удаление старых векторов с таким же short_name перед добавлением новых)
        try:
            collection.delete(where={"short_name": short_name})
            log_info("ChromaDB", f"Удалены старые записи с short_name='{short_name}' из коллекции {collection_name} перед upsert")
        except Exception as e:
            log_warning("ChromaDB", f"Предупреждение при удалении старых векторов: {e}")

        # 4-7. Нарезка и Векторизация
        clean_text = markdown_text
        if clean_text.startswith("---"):
            parts = clean_text.split("---", 2)
            if len(parts) >= 3:
                clean_text = parts[2].strip()

        # Режем текст на чанки
        splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
        chunks = splitter.split_text(clean_text)
        log_info("Text Splitter", f"Текст разбит на {len(chunks)} чанков.")
        
        # Батчинг (обход лимитов)
        for i in range(0, len(chunks), CHUNK_BATCH_SIZE):
            batch_chunks = chunks[i:i + CHUNK_BATCH_SIZE]
            log_info("Batching", f"Обработка батча {i//CHUNK_BATCH_SIZE + 1} (чанки {i+1} - {i+len(batch_chunks)})...")
            
            try:
                embeddings = get_embeddings_google(batch_chunks)
            except Exception as e:
                log_error("Google API", f"Ошибка получения эмбеддингов от Vertex AI: {e}")
                return
                
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
                
        log_success("ChromaDB", f"Успешно записано {len(chunks)} векторов для '{short_name}' в {collection_name}")
        
    # 8. Уборка оригинального файла из MinIO (выполняется и для upsert, и для delete)
    try:
        minio_client.remove_object(bucket, file_path)
        log_info("MinIO", f"Оригинальный файл {file_path} удален из MinIO.")
    except Exception as e:
        log_warning("MinIO", f"Предупреждение при удалении из MinIO: {e}")

def main():
    log_success("Worker Start", "Воркер запущен и слушает Redis (document_tasks)...")
    while True:
        try:
            # Блокирующее чтение из очереди Redis (brpop = FIFO)
            task_data = redis_client.brpop("document_tasks", timeout=0)
            if task_data:
                _, message_json = task_data
                task = json.loads(message_json)
                process_task(task)
        except Exception as e:
            log_error("Worker Error", f"Ошибка в главном цикле воркера: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
