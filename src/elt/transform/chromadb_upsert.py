import os
import sys
import json
import time
import asyncio
import base64
import io
from datetime import datetime
from colorama import Fore, Style
from dotenv import load_dotenv

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
from src.core.app_clients import AppClients
# pyrefly: ignore [missing-import]
from langchain_text_splitters import RecursiveCharacterTextSplitter

class ChromaDBUpsert:
    def __init__(self):
        load_dotenv()
        self.rag_bucket = "rag-documents"
        self.upsert_prefix = "upsert/"
        self.minio_client = AppClients.get_minio_client()
        self.chroma_client = AppClients.get_chroma_db()
        self.embedder = AppClients.get_embedder_client()
        
        self.collection = self.chroma_client.get_or_create_collection(
            name="global_rules",
            embedding_function=self.embedder
        )
        
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=2000,
            chunk_overlap=300,
            length_function=len
        )
        
        self.max_concurrency = 3
        self.semaphore = asyncio.Semaphore(self.max_concurrency)
        self.embedding_batch_size = 200
        
        self.cache_dir = "data/cache/transform"
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cache_file = os.path.join(self.cache_dir, "upsert_cache.json")
        self.cache_lock = asyncio.Lock()

    def _log(self, level: str, message: str):
        colors = {
            "INFO": Fore.CYAN,
            "SUCCESS": Fore.GREEN,
            "WARNING": Fore.YELLOW,
            "ERROR": Fore.RED
        }
        color = colors.get(level, Fore.WHITE)
        print(f"{color}[{level}]:{Style.RESET_ALL} {message}")

    async def _load_cache(self) -> dict:
        async with self.cache_lock:
            if not os.path.exists(self.cache_file):
                return {}
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except json.JSONDecodeError:
                return {}

    async def _save_cache(self, file_name: str, data: dict):
        async with self.cache_lock:
            cache = {}
            if os.path.exists(self.cache_file):
                try:
                    with open(self.cache_file, "r", encoding="utf-8") as f:
                        cache = json.load(f)
                except json.JSONDecodeError:
                    pass
            cache[file_name] = data
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=4)

    async def process_file(self, object_name: str):
        async with self.semaphore:
            self._log("INFO", f"Начало обработки {object_name}")
            cache_info = {
                "start_time": datetime.now().isoformat(),
                "status": "processing"
            }
            try:
                # 1. Скачивание файла и метаданных
                response = self.minio_client.get_object(self.rag_bucket, object_name)
                content = response.read().decode("utf-8")
                
                stat = self.minio_client.stat_object(self.rag_bucket, object_name)
                minio_meta = stat.metadata or {}
                
                # Извлекаем наши метаданные (MinIO добавляет префикс X-Amz-Meta-)
                def safe_b64decode(val: str, default: str) -> str:
                    try:
                        return base64.b64decode(val).decode('utf-8')
                    except Exception:
                        return val or default
                        
                official_name = safe_b64decode(minio_meta.get("x-amz-meta-official-name", ""), "Unknown Document")
                sign_date = minio_meta.get("x-amz-meta-sign-date", "")
                short_number = safe_b64decode(minio_meta.get("x-amz-meta-short-number", ""), "")
                
                response.close()
                response.release_conn()

                # 2. Нарезаем на чанки
                chunks = self.text_splitter.split_text(content)
                if not chunks:
                    self._log("WARNING", f"Файл {object_name} пустой после чанкинга.")
                    self.minio_client.remove_object(self.rag_bucket, object_name)
                    cache_info["status"] = "empty"
                    await self._save_cache(object_name, cache_info)
                    return

                # 3. Подготовка данных для Chroma
                documents = chunks
                base_name = os.path.basename(object_name)
                ids = [f"{base_name}_chunk_{i}" for i in range(len(chunks))]
                
                metadatas = [
                    {
                        "source": base_name,
                        "official_name": official_name,
                        "sign_date": sign_date,
                        "short_number": short_number
                    } for _ in chunks
                ]

                # 4. Векторизация и загрузка (батчами)
                for i in range(0, len(documents), self.embedding_batch_size):
                    batch_docs = documents[i:i + self.embedding_batch_size]
                    batch_ids = ids[i:i + self.embedding_batch_size]
                    batch_metas = metadatas[i:i + self.embedding_batch_size]
                    
                    # Делаем паузу для соблюдения лимитов API
                    await asyncio.sleep(2.0)
                    
                    # Upsert 
                    await asyncio.to_thread(
                        self.collection.upsert,
                        documents=batch_docs,
                        ids=batch_ids,
                        metadatas=batch_metas
                    )
                
                # 5. Удаляем файл из MinIO
                self.minio_client.remove_object(self.rag_bucket, object_name)
                
                self._log("SUCCESS", f"Файл {object_name} ({len(chunks)} чанков) успешно загружен в ChromaDB.")
                cache_info["status"] = "success"
                cache_info["chunks"] = len(chunks)
                cache_info["end_time"] = datetime.now().isoformat()
                await self._save_cache(object_name, cache_info)
                
            except Exception as e:
                self._log("ERROR", f"Ошибка при обработке {object_name}: {str(e)}")
                cache_info["status"] = "error"
                cache_info["error"] = str(e)
                cache_info["end_time"] = datetime.now().isoformat()
                await self._save_cache(object_name, cache_info)

    async def run(self):
        self._log("INFO", "Запуск Transform Layer (Upsert)...")
        objects = list(self.minio_client.list_objects(self.rag_bucket, prefix=self.upsert_prefix, recursive=True))
        
        files_to_process = [obj.object_name for obj in objects if not obj.object_name.endswith("/")]
        
        if not files_to_process:
            self._log("INFO", "Очередь upsert пуста.")
            return

        self._log("INFO", f"Найдено {len(files_to_process)} файлов для загрузки.")
        
        tasks = []
        for obj_name in files_to_process:
            tasks.append(self.process_file(obj_name))
            
        await asyncio.gather(*tasks)
        self._log("SUCCESS", "Пайплайн Upsert завершил работу.")

if __name__ == "__main__":
    pipeline = ChromaDBUpsert()
    asyncio.run(pipeline.run())
