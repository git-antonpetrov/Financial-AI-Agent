import os
import json
import time
import requests
import redis
from minio import Minio
import chromadb
from chromadb.config import Settings
from langchain_text_splitters import RecursiveCharacterTextSplitter

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
print("Инициализация клиентов...")

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
print("⏳ Ожидание ChromaDB...")
for attempt in range(30):
    try:
        chroma_client.heartbeat()
        print("✅ ChromaDB доступна.")
        break
    except Exception:
        time.sleep(2)
else:
    print("❌ ChromaDB не отвечает после 30 попыток. Завершение.")
    exit(1)

def get_embeddings_google(texts: list[str]) -> list[list[float]]:
    """
    Отправляет батч текстов к Vertex AI (через наш прокси) для получения эмбеддингов.
    Формат соответствует gemini-embedding-2 (batchEmbedContents).
    """
    if not VERTEX_BASE_URL:
        raise ValueError("VERTEX_BASE_URL не настроен в .env")

    url = f"{VERTEX_BASE_URL}/v1beta1/projects/{VERTEX_PROJECT}/locations/{VERTEX_LOCATION}/publishers/google/models/{EMBEDDING_MODEL}:batchEmbedContents"
    
    # Формируем тело запроса по стандарту Vertex AI batchEmbedContents
    requests_payload = [
        {
            "content": {"parts": [{"text": text}]},
            "taskType": "RETRIEVAL_DOCUMENT",
            "model": f"models/{EMBEDDING_MODEL}"
        }
        for text in texts
    ]
    
    payload = {"requests": requests_payload}
    
    response = requests.post(url, json=payload, headers={"Content-Type": "application/json"})
    response.raise_for_status()
    
    data = response.json()
    embeddings = []
    
    if "embeddings" in data:
        for emb in data["embeddings"]:
            if "values" in emb:
                embeddings.append(emb["values"])
            else:
                raise ValueError("Неверный формат ответа от Google API: отсутствует поле 'values'")
    else:
        raise ValueError(f"Неверный формат ответа от Google API (нет 'embeddings'): {data}")
        
    return embeddings

def extract_metadata_from_markdown(markdown_text: str, filename: str) -> dict:
    """
    Извлекает метаданные из начала Markdown файла.
    Если не найдено, пытается вытащить short_name из имени файла (например, 115-fz.md).
    """
    meta = {"short_name": filename.replace(".md", "").split("/")[-1]}
    
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
    
    print(f"\n--- Новая задача: [{action}] Агент: {agent}, Файл: {file_path}")
    
    # 1. Скачиваем файл из MinIO
    try:
        response = minio_client.get_object(bucket, file_path)
        markdown_text = response.read().decode('utf-8')
    except Exception as e:
        print(f"❌ Ошибка скачивания файла {file_path} из MinIO: {e}")
        return
    finally:
        if 'response' in locals():
            response.close()
            
    # 2. Парсинг метаданных (чтобы достать short_name)
    metadata = extract_metadata_from_markdown(markdown_text, file_path)
    short_name = metadata.get("short_name")
    
    if not short_name:
        print("❌ Ошибка: не удалось определить short_name для документа.")
        return
        
    print(f"📄 Документ распознан. short_name: '{short_name}'")
    
    # Получаем/создаем коллекцию для конкретного агента
    collection_name = f"knowledge-{agent}"
    collection = chroma_client.get_or_create_collection(name=collection_name)
    
    # 3. Очистка (Удаление старых векторов с таким же short_name)
    try:
        collection.delete(where={"short_name": short_name})
        print(f"🗑 Удалены старые записи с short_name='{short_name}' из коллекции {collection_name}")
    except Exception as e:
        print(f"Предупреждение при удалении старых векторов: {e}")

    if action == "delete":
        print("✅ Задача delete выполнена.")
    
    # 4-7. Нарезка и Векторизация (upsert)
    elif action == "upsert":
        clean_text = markdown_text
        if clean_text.startswith("---"):
            parts = clean_text.split("---", 2)
            if len(parts) >= 3:
                clean_text = parts[2].strip()

        # Режем текст на чанки
        splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
        chunks = splitter.split_text(clean_text)
        print(f"🔪 Текст разбит на {len(chunks)} чанков.")
        
        # Батчинг (обход лимитов)
        for i in range(0, len(chunks), CHUNK_BATCH_SIZE):
            batch_chunks = chunks[i:i + CHUNK_BATCH_SIZE]
            print(f"🔄 Обработка батча {i//CHUNK_BATCH_SIZE + 1} (чанки {i+1} - {i+len(batch_chunks)})...")
            
            try:
                embeddings = get_embeddings_google(batch_chunks)
            except Exception as e:
                print(f"❌ Ошибка получения эмбеддингов от Vertex AI: {e}")
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
                print(f"⏳ Ждем {CHUNK_SLEEP_SECONDS} сек. для сброса лимитов Google...")
                time.sleep(CHUNK_SLEEP_SECONDS)
                
        print(f"✅ Успешно записано {len(chunks)} векторов для '{short_name}' в {collection_name}")
        
    # 8. Уборка оригинального файла из MinIO (выполняется и для upsert, и для delete)
    try:
        minio_client.remove_object(bucket, file_path)
        print(f"🧹 Оригинальный файл {file_path} удален из MinIO.")
    except Exception as e:
        print(f"Предупреждение при удалении из MinIO: {e}")

def main():
    print("🚀 Воркер запущен и слушает Redis (document_tasks)...")
    while True:
        try:
            # Блокирующее чтение из очереди Redis (brpop = FIFO)
            task_data = redis_client.brpop("document_tasks", timeout=0)
            if task_data:
                _, message_json = task_data
                task = json.loads(message_json)
                process_task(task)
        except Exception as e:
            print(f"❌ Ошибка в главном цикле воркера: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
