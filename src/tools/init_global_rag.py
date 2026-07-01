import os
import sys
import glob
import time

sys.stdout.reconfigure(encoding='utf-8')
import asyncio
# pyrefly: ignore [missing-import]
from litellm import aembedding

MAX_NETWORK_CONCURRENCY = 15
MAX_IO_CONCURRENCY = 50
EMBEDDING_BATCH_SIZE = 200

sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
# pyrefly: ignore [missing-import]
from src.app_clients import AppClients
# pyrefly: ignore [missing-import]
from src.tools.metadata_generator import extract_document_title
# pyrefly: ignore [missing-import]
from langchain_text_splitters import RecursiveCharacterTextSplitter
# pyrefly: ignore [missing-import]
from langchain_community.document_loaders import (
    TextLoader,
    PyPDFLoader,
    Docx2txtLoader,
    UnstructuredWordDocumentLoader,
    UnstructuredODTLoader,
    UnstructuredMarkdownLoader,
)

def extract_text_from_file(filepath: str) -> str:
    ext = os.path.splitext(filepath)[1].lower()
    try:
        match ext:
            case '.txt':
                loader = TextLoader(filepath, encoding='utf-8')
            case '.pdf':
                loader = PyPDFLoader(filepath)
            case '.docx':
                loader = Docx2txtLoader(filepath)
            case '.doc':
                loader = UnstructuredWordDocumentLoader(filepath)
            case '.odt':
                loader = UnstructuredODTLoader(filepath)
            case '.md':
                loader = UnstructuredMarkdownLoader(filepath)
            case _:
                print(f"Logs: \033[93m[Предупреждение]\033[0m Формат {ext} не поддерживается: {filepath}")
                return ""
                
        docs = loader.load()
        return "\n".join(doc.page_content for doc in docs)
    except Exception as e:
        print(f"Logs: \033[91m[Ошибка]\033[0m Ошибка при чтении файла {filepath}: {e}")
        return ""

async def process_file(filepath, text_splitter, collection, io_sem: asyncio.Semaphore, network_sem: asyncio.Semaphore):
    print(f"Logs: \033[96m[Процесс]\033[0m Читаем файл: {filepath}")
    t0 = time.perf_counter()
    
    async with io_sem:
        text = await asyncio.to_thread(extract_text_from_file, filepath)
        
    t1 = time.perf_counter()
    parse_time = t1 - t0
        
    if not text.strip():
        print(f"Logs: \033[93m[Пропуск]\033[0m Файл {filepath} пуст")
        return 0
        
    # Нарезаем на чанки
    chunks = text_splitter.split_text(text)
    filename = os.path.basename(filepath)
    
    async with network_sem:
        source_title = await extract_document_title(text, filename)
        
    t2 = time.perf_counter()
    meta_time = t2 - t1
    
    documents = chunks
    ids = [f"{filename}_chunk_{i}" for i in range(len(chunks))]
    metadatas = [{"source": source_title, "raw_filename": filename} for _ in chunks]
    
    # Асинхронная векторизация батчами
    async def _safe_embed(batch):
        async with network_sem:
            return await aembedding(
                model=os.getenv("EMBEDDING_PROVIDER_MODEL", "vertex_ai/gemini-embedding-001"),
                input=batch,
                vertex_project=os.getenv("VERTEX_PROJECT"),
                vertex_location=os.getenv("VERTEX_LOCATION")
            )

    embedding_tasks = []
    for i in range(0, len(chunks), EMBEDDING_BATCH_SIZE):
        batch = chunks[i:i + EMBEDDING_BATCH_SIZE]
        embedding_tasks.append(_safe_embed(batch))
    
    responses = await asyncio.gather(*embedding_tasks)
    
    file_embeddings = []
    for response in responses:
        batch_embeddings = [item["embedding"] for item in response["data"]]
        file_embeddings.extend(batch_embeddings)
    
    # Загружаем в базу (Изолируем блокирующий I/O в отдельный поток)
    async with io_sem:
        await asyncio.to_thread(
            collection.upsert,
            documents=documents,
            ids=ids,
            metadatas=metadatas,
            embeddings=file_embeddings
        )
        
    t3 = time.perf_counter()
    embed_time = t3 - t2
    
    print(f"Logs: \033[92m[Успех]\033[0m Файл {filepath} загружен ({len(chunks)} фрагментов)")
    print(f"Stats: \033[94m[Профилирование]\033[0m Чтение: {parse_time:.2f}с | Метаданные (LLM): {meta_time:.2f}с | Векторизация: {embed_time:.2f}с")
    return len(chunks)

async def main_async():
    print("Logs: \033[92m[Старт]\033[0m Начинаем индексацию базы знаний...")
    global_t0 = time.perf_counter()
    
    # 1. Получаем клиент ChromaDB
    client = AppClients.get_chroma_db()
    
    # 2. Инициализируем коллекцию
    embedder = AppClients.get_embedder_client()
    collection = client.get_or_create_collection(
        name="global_rules",
        embedding_function=embedder
    )
    
    # 3. Ищем файлы
    kb_dir = "knowledge_base"
    SUPPORTED_EXTENSIONS = ('.txt', '.md', '.pdf', '.docx', '.doc', '.odt')
    
    all_files = glob.glob(os.path.join(kb_dir, "*"))
    files_to_index = [f for f in all_files if os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS]
    
    if not files_to_index:
        print("Logs: \033[91m[Ошибка]\033[0m База знаний пуста (нет подходящих файлов)")
        return

    # 4. Настраиваем сплиттер
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=2000,
        chunk_overlap=300,
        length_function=len
    )
    
    io_sem = asyncio.Semaphore(MAX_IO_CONCURRENCY)
    network_sem = asyncio.Semaphore(MAX_NETWORK_CONCURRENCY)
    tasks = [process_file(filepath, text_splitter, collection, io_sem, network_sem) for filepath in files_to_index]
    results = await asyncio.gather(*tasks)
    total_chunks = sum(results)

    global_t1 = time.perf_counter()
    total_time = global_t1 - global_t0
    
    print(f"Logs: \033[92m[Финиш]\033[0m Индексация завершена. Всего фрагментов: {total_chunks}")
    print(f"Stats: \033[94m[Общее время]\033[0m Затрачено времени: {total_time:.2f}с")

if __name__ == "__main__":
    asyncio.run(main_async())
