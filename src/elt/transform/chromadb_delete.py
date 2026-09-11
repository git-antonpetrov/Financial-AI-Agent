import os
import sys
import json
import asyncio
from datetime import datetime
from dotenv import load_dotenv

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
from src.core.app_clients import AppClients
from src.core.models import TransformState
from src.utils.console_logger import log_info, log_error, log_warning
from sqlalchemy import select

class ChromaDBDelete:
    """
    Класс для удаления устаревших векторов из ChromaDB.
    Удаляет векторы для документов, чьи номера (short_number) числятся в файлах отмены в MinIO.
    """
    def __init__(self):
        load_dotenv()
        self.rag_bucket = "rag-documents"
        self.delete_prefix = "delete/"
        self.minio_client = AppClients.get_minio_client()
        self.chroma_client = AppClients.get_chroma_db()
        self.embedder = AppClients.get_embedder_client()
        self.db_session_maker = AppClients.get_async_session()
        
        self.collection = self.chroma_client.get_or_create_collection(
            name="global_rules",
            embedding_function=self.embedder
        )
        
        self.max_concurrency = 3
        self.semaphore = asyncio.Semaphore(self.max_concurrency)

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
        Обрабатывает один файл со списком отмененных номеров.
        Удаляет соответствующие векторы из базы.
        
        Args:
            object_name (str): Имя объекта в MinIO (с префиксом delete/).
        """
        async with self.semaphore:
            log_info("ChromaDB Delete", f"Начало обработки удаления файла {object_name}")
            
            async with self.db_session_maker() as db:
                new_state = TransformState(
                    file_name=object_name,
                    transform_type="delete",
                    started_at=datetime.now(),
                    status="processing"
                )
                db.add(new_state)
                await db.commit()
                await db.refresh(new_state)
                state_id = new_state.id
                
            try:
                # 1. Читается JSON со списком отмененных номеров
                response = self.minio_client.get_object(self.rag_bucket, object_name)
                content = response.read().decode("utf-8")
                response.close()
                response.release_conn()
                
                repealed_list = json.loads(content)
                if not isinstance(repealed_list, list):
                    raise ValueError("JSON не является списком")

                # 2. Удаляются векторы из ChromaDB по short_number
                deleted_count = 0
                for short_number in repealed_list:
                    if not short_number:
                        continue
                    
                    # ChromaDB поддерживает удаление по условиям (where clause)
                    # Вызов делается синхронно в потоке
                    await asyncio.to_thread(
                        self.collection.delete,
                        where={"short_number": short_number}
                    )
                    log_info("ChromaDB Delete", f"Удалены векторы для отмененного документа: {short_number}")
                    deleted_count += 1

                # 3. Удаляется JSON файл из MinIO
                self.minio_client.remove_object(self.rag_bucket, object_name)
                
                log_info("ChromaDB Delete", f"Файл {object_name} успешно обработан. Выполнено удалений: {deleted_count}")
                await self._update_db_state(state_id, status="completed", completed_at=datetime.now())
                
            except Exception as e:
                log_error("ChromaDB Delete", f"Ошибка при обработке {object_name}: {str(e)}")
                await self._update_db_state(state_id, status="error", error_message=str(e)[:500], completed_at=datetime.now())

    async def run(self):
        """
        Главная точка входа. Находит все файлы в папке delete/ и обрабатывает их.
        """
        log_info("ChromaDB Delete", "Запуск Transform Layer (Delete)...")
        objects = list(self.minio_client.list_objects(self.rag_bucket, prefix=self.delete_prefix, recursive=True))
        
        files_to_process = [obj.object_name for obj in objects if not obj.object_name.endswith("/")]
        
        if not files_to_process:
            log_info("ChromaDB Delete", "Очередь delete пуста.")
            return

        log_info("ChromaDB Delete", f"Найдено {len(files_to_process)} файлов на удаление.")
        
        tasks = []
        for obj_name in files_to_process:
            tasks.append(self.process_file(obj_name))
            
        await asyncio.gather(*tasks)
        log_info("ChromaDB Delete", "Пайплайн Delete завершил работу.")

if __name__ == "__main__":
    pipeline = ChromaDBDelete()
    asyncio.run(pipeline.run())
