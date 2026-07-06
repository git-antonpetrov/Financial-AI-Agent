import os
import sys
import glob
import time
import json
import hashlib
import asyncio
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8')
# pyrefly: ignore [missing-import]
from litellm import aembedding
from colorama import Fore, Style
import fitz  # PyMuPDF

sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
# pyrefly: ignore [missing-import]
from src.core.app_clients import AppClients
# pyrefly: ignore [missing-import]
from src.agent_tools.metadata_generator import extract_document_title
# pyrefly: ignore [missing-import]
from langchain_text_splitters import RecursiveCharacterTextSplitter
from src.utils.content_ai_recognizer import ContentCaptureRecognizer

MAX_NETWORK_CONCURRENCY = 20
MAX_CONTENT_AI_CONCURRENCY = 10
MAX_FILE_CONCURRENCY = 20
MAX_IO_CONCURRENCY = 5
EMBEDDING_BATCH_SIZE = 200

def get_file_md5(filepath: str) -> str:
    hasher = hashlib.md5()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hasher.update(chunk)
    return hasher.hexdigest()

class RagCache:
    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        self.cache_file = os.path.join(cache_dir, "progress.json")
        self.lock = asyncio.Lock()
        os.makedirs(self.cache_dir, exist_ok=True)
        if os.path.exists(self.cache_file):
            with open(self.cache_file, "r", encoding="utf-8") as f:
                try:
                    self.data = json.load(f)
                    if isinstance(self.data, list):
                        self.data = {h: "unknown" for h in self.data}
                except json.JSONDecodeError:
                    self.data = {}
        else:
            self.data = {}

    def is_processed(self, file_hash: str) -> bool:
        return file_hash in self.data

    async def mark_processed(self, file_hash: str, filename: str):
        async with self.lock:
            if file_hash not in self.data:
                self.data[file_hash] = filename
                # Run file write in thread
                await asyncio.to_thread(self._write_cache)
                
    def _write_cache(self):
        with open(self.cache_file, "w", encoding="utf-8") as f:
            json.dump(self.data, f)

def extract_text_fitz(filepath: str) -> str:
    """Extracts text using PyMuPDF. Returns text if it looks like a text PDF, else empty string."""
    try:
        doc = fitz.open(filepath)
        total_pages = len(doc)
        if total_pages == 0:
            return ""
        
        text_pages = []
        for i in range(min(5, total_pages)): # check up to 5 pages
            text_pages.append(doc[i].get_text())
            
        full_sample_text = "".join(text_pages)
        # Heuristic: if average chars per page is < 100, it's likely a scanned PDF
        if len(full_sample_text) / max(1, len(text_pages)) < 100:
            doc.close()
            return ""
            
        # It's a text PDF, extract everything
        all_text = [page.get_text() for page in doc]
        doc.close()
        return "\n".join(all_text)
    except Exception as e:
        print(f"{Fore.RED}[Ошибка]{Style.RESET_ALL} PyMuPDF не смог прочитать {filepath}: {e}")
        return ""

def extract_text_content_ai(filepath: str) -> str:
    recognizer = ContentCaptureRecognizer(delete_batch_after=True)
    result_dict = recognizer.recognize(filepath)
    full_text = ""
    for doc_id, data in result_dict.items():
        if "_FullText" in data:
            full_text += data["_FullText"] + "\n\n"
    return full_text

async def process_file(
    filepath: str, 
    text_splitter, 
    collection, 
    cache: RagCache,
    file_sem: asyncio.Semaphore,
    io_sem: asyncio.Semaphore, 
    network_sem: asyncio.Semaphore,
    content_ai_sem: asyncio.Semaphore
):
    async with file_sem:
        filename = os.path.basename(filepath)
        
        # 1. Calc MD5
        async with io_sem:
            file_hash = await asyncio.to_thread(get_file_md5, filepath)
            
        if cache.is_processed(file_hash):
            print(f"Logs: {Fore.YELLOW}[Кэш]{Style.RESET_ALL} Файл {filename} уже обработан, пропускаем")
            return 0
            
        print(f"Logs: {Fore.CYAN}[Анализ]{Style.RESET_ALL} Файл: {filename}")
        t0 = time.perf_counter()
        
        # 2. Try fast text extraction
        async with io_sem:
            text = await asyncio.to_thread(extract_text_fitz, filepath)
            
        extraction_method = "PyMuPDF"
        if not text.strip():
            print(f"Logs: {Fore.MAGENTA}[Распознавание]{Style.RESET_ALL} Файл {filename} - это скан, отправляем в Content AI...")
            extraction_method = "Content AI"
            # Acquire Content AI semaphore
            async with content_ai_sem:
                try:
                    text = await asyncio.to_thread(extract_text_content_ai, filepath)
                except Exception as e:
                    print(f"Logs: {Fore.RED}[Ошибка]{Style.RESET_ALL} Content AI упал на {filename}: {e}")
                    return 0
                    
        t1 = time.perf_counter()
        parse_time = t1 - t0
        
        if not text.strip():
            print(f"Logs: {Fore.RED}[Ошибка]{Style.RESET_ALL} Файл {filename} пуст после всех методов извлечения")
            return 0
            
        # 3. Нарезаем на чанки
        chunks = text_splitter.split_text(text)
        if not chunks:
            return 0
            
        # 4. LLM Metadata
        async with network_sem:
            source_title = await extract_document_title(text, filename)
            
        t2 = time.perf_counter()
        meta_time = t2 - t1
        
        documents = chunks
        ids = [f"{file_hash}_chunk_{i}" for i in range(len(chunks))]
        metadatas = [{"source": source_title, "raw_filename": filename} for _ in chunks]
        
        # 5. Асинхронная векторизация батчами
        async def _safe_embed(batch):
            async with network_sem:
                # Add delay to respect RPM (1250 RPM = ~20 RPS)
                await asyncio.sleep(0.5)
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
        
        try:
            responses = await asyncio.gather(*embedding_tasks)
        except Exception as e:
            print(f"Logs: {Fore.RED}[Ошибка]{Style.RESET_ALL} Ошибка векторизации {filename}: {e}")
            return 0
            
        file_embeddings = []
        for response in responses:
            batch_embeddings = [item["embedding"] for item in response["data"]]
            file_embeddings.extend(batch_embeddings)
        
        # 6. Загружаем в базу
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
        
        # 7. Записываем в кэш
        await cache.mark_processed(file_hash, filename)
        
        print(f"Logs: {Fore.GREEN}[Успех]{Style.RESET_ALL} Файл {filename} загружен ({len(chunks)} фрагментов)")
        print(f"Stats: {Fore.BLUE}[Профилирование]{Style.RESET_ALL} Извлечение ({extraction_method}): {parse_time:.2f}с | LLM: {meta_time:.2f}с | Векторизация: {embed_time:.2f}с")
        return len(chunks)

async def main_async():
    load_dotenv()
    print(f"Logs: {Fore.GREEN}[Старт]{Style.RESET_ALL} Начинаем индексацию базы знаний...")
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
    kb_dir = "data/2_storage"
    cache_dir = "data/.rag_cache"
    cache = RagCache(cache_dir)
    
    all_files = glob.glob(os.path.join(kb_dir, "*.pdf"))
    
    if not all_files:
        print(f"Logs: {Fore.RED}[Ошибка]{Style.RESET_ALL} База знаний пуста (нет .pdf файлов в 2_storage)")
        return

    # 4. Настраиваем сплиттер
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=2000,
        chunk_overlap=300,
        length_function=len
    )
    
    file_sem = asyncio.Semaphore(MAX_FILE_CONCURRENCY)
    io_sem = asyncio.Semaphore(MAX_IO_CONCURRENCY)
    network_sem = asyncio.Semaphore(MAX_NETWORK_CONCURRENCY)
    content_ai_sem = asyncio.Semaphore(MAX_CONTENT_AI_CONCURRENCY)
    
    tasks = [process_file(filepath, text_splitter, collection, cache, file_sem, io_sem, network_sem, content_ai_sem) for filepath in all_files]
    results = await asyncio.gather(*tasks)
    total_chunks = sum(results)

    global_t1 = time.perf_counter()
    total_time = global_t1 - global_t0
    
    print(f"Logs: {Fore.GREEN}[Финиш]{Style.RESET_ALL} Индексация завершена. Всего фрагментов добавлено: {total_chunks}")
    print(f"Stats: {Fore.BLUE}[Общее время]{Style.RESET_ALL} Затрачено времени: {total_time:.2f}с")

if __name__ == "__main__":
    asyncio.run(main_async())
