import os
import sys
import json
import time
import asyncio
import base64
import io
from datetime import datetime
from dotenv import load_dotenv
from typing import Callable, Any

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
from src.elt.db.models import TransformState
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
# pyrefly: ignore [missing-import]
from langchain_text_splitters import RecursiveCharacterTextSplitter
from src.core.utils.console_logger import log_info, log_error, log_warning
from minio import Minio

class ChromaDBUpsert:
    """
    Класс для добавления новых векторов в ChromaDB.
    Берет размеченные Markdown файлы из MinIO, нарезает их на чанки (TextSplitter)
    и отправляет в ChromaDB для векторизации и сохранения.
    """
    def __init__(
        self,
        db_session_maker: Callable[..., AsyncSession],
        minio_client: Minio,
        chroma_client: Any,
        embedder: Any
    ):
        load_dotenv()
        self.rag_bucket = "rag-documents"
        self.upsert_prefix = "upsert/"
        self.minio_client = minio_client
        self.chroma_client = chroma_client
        self.embedder = embedder
        self.db_session_maker = db_session_maker
        
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

    async def _update_db_state(self, state_id: str, **kwargs):
        """Обновляет запись о состоянии трансформации в БД."""
        async with self.db_session_maker() as db:
            result = await db.execute(select(TransformState).where(TransformState.id == state_id))
            state = result.scalar_one_or_none()
            if state:
                for key, value in kwargs.items():
                    setattr(state, key, value)
                await db.commit()

    async def process_file(self, object_name: str):
        """
        Обрабатывает один файл для загрузки в базу.
        Скачивает его, извлекает метаданные, нарезает на чанки и батчами отправляет в БД.
        
        Args:
            object_name (str): Имя объекта в MinIO (с префиксом upsert/).
        """
        async with self.semaphore:
            log_info("ChromaDB Upsert", f"Начало обработки {object_name}")
            
            async with self.db_session_maker() as db:
                new_state = TransformState(
                    file_name=object_name,
                    transform_type="upsert",
                    started_at=datetime.now(),
                    status="processing"
                )
                db.add(new_state)
                await db.commit()
                await db.refresh(new_state)
                state_id = new_state.id
                
            try:
                # 1. Скачивание файла и метаданных
                response = self.minio_client.get_object(self.rag_bucket, object_name)
                content = response.read().decode("utf-8")
                
                stat = self.minio_client.stat_object(self.rag_bucket, object_name)
                minio_meta = stat.metadata or {}
                
                # Извлекаются наши метаданные (MinIO добавляет префикс X-Amz-Meta-)
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

                # 2. Нарезка на чанки
                chunks = self.text_splitter.split_text(content)
                if not chunks:
                    log_warning("ChromaDB Upsert", f"Файл {object_name} пустой после чанкинга.")
                    self.minio_client.remove_object(self.rag_bucket, object_name)
                    await self._update_db_state(state_id, status="empty", completed_at=datetime.now())
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
                    
                    # Делается пауза для соблюдения лимитов API
                    await asyncio.sleep(2.0)
                    
                    # Upsert 
                    await asyncio.to_thread(
                        self.collection.upsert,
                        documents=batch_docs,
                        ids=batch_ids,
                        metadatas=batch_metas
                    )
                
                # 5. Удаляется файл из MinIO
                self.minio_client.remove_object(self.rag_bucket, object_name)
                
                log_info("ChromaDB Upsert", f"Файл {object_name} ({len(chunks)} чанков) успешно загружен в ChromaDB.")
                await self._update_db_state(state_id, status="completed", chunks_count=len(chunks), completed_at=datetime.now())
                
            except Exception as e:
                log_error("ChromaDB Upsert", f"Ошибка при обработке {object_name}: {str(e)}")
                await self._update_db_state(state_id, status="error", error_message=str(e)[:500], completed_at=datetime.now())

    async def run(self):
        """
        Главная точка входа. Находит все файлы в папке upsert/ и обрабатывает их.
        """
        log_info("ChromaDB Upsert", "Запуск Transform Layer (Upsert)...")
        objects = list(self.minio_client.list_objects(self.rag_bucket, prefix=self.upsert_prefix, recursive=True))
        
        files_to_process = [obj.object_name for obj in objects if not obj.object_name.endswith("/")]
        
        if not files_to_process:
            log_info("ChromaDB Upsert", "Очередь upsert пуста.")
            return

        log_info("ChromaDB Upsert", f"Найдено {len(files_to_process)} файлов для загрузки.")
        
        tasks = []
        for obj_name in files_to_process:
            tasks.append(self.process_file(obj_name))
            
        await asyncio.gather(*tasks)
        log_info("ChromaDB Upsert", "Пайплайн Upsert завершил работу.")

def start_upsert(db_session_maker: Callable[..., AsyncSession], minio_client: Minio, chroma_client: Any, embedder: Any):
    pipeline = ChromaDBUpsert(db_session_maker, minio_client, chroma_client, embedder)
    asyncio.run(pipeline.run())

if __name__ == "__main__":
    from src.core.clients.db import get_async_session_maker
    from src.core.clients.storage import get_minio_client
    from src.core.clients.vector_db import get_chroma_client
    from src.core.clients.llm import get_embedder

    db_maker = get_async_session_maker()
    minio = get_minio_client()
    chroma = get_chroma_client()
    embedder_func = get_embedder()
    
    start_upsert(db_maker, minio, chroma, embedder_func)
