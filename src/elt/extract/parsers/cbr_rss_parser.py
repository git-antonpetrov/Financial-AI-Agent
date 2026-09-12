import os
import sys
import uuid
import urllib3
import re
import asyncio
import aiohttp
from datetime import datetime, date
from io import BytesIO
from urllib.parse import unquote
import xml.etree.ElementTree as ET
from typing import Callable

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))
from src.core.utils.console_logger import log_info, log_warning, log_error
from src.elt.db.models import ExtractState
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
# pyrefly: ignore [missing-import]
from minio.error import S3Error
from minio import Minio

# Исправление кодировки консоли Windows
sys.stdout.reconfigure(encoding='utf-8')

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class CbrRssParser:
    """
    Парсер для загрузки документов из RSS-ленты Центрального Банка РФ.
    Скачивает файлы и сохраняет их в бакет MinIO, а также логирует состояние в базу данных PostgreSQL.
    """
    def __init__(self, db_session_maker: Callable[..., AsyncSession], minio_client: Minio, base_dir: str):
        self.base_dir = base_dir
        self.db_session_maker = db_session_maker
        self.minio_client = minio_client
        
        self.rss_url = "https://www.cbr.ru/rss/navr"
        
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7'
        }
        
        self.minio_bucket = "raw-documents"
        self._ensure_bucket_exists()

    def _ensure_bucket_exists(self):
        """Проверяет существование бакета в MinIO и создает его при необходимости."""
        try:
            if not self.minio_client.bucket_exists(self.minio_bucket):
                self.minio_client.make_bucket(self.minio_bucket)
                log_info("CBR Парсер", f"Создан бакет: {self.minio_bucket}")
        except Exception as e:
            log_error("MinIO", f"Не удалось проверить или создать бакет: {e}")

    def upload_to_minio(self, content: bytes, original_filename: str) -> bool:
        """Синхронная функция загрузки в MinIO. Вызывается через asyncio.to_thread."""
        try:
            self.minio_client.put_object(
                self.minio_bucket,
                original_filename,
                data=BytesIO(content),
                length=len(content),
                content_type="application/pdf"
            )
            log_info("MinIO", f"Файл загружен: {original_filename}")
            return True
        except S3Error as e:
            log_warning("MinIO", f"Ошибка при загрузке с именем '{original_filename}': {e}. Пробуется автоматическое имя...")
            try:
                fallback_name = f"cbr_doc_{uuid.uuid4().hex}.pdf"
                self.minio_client.put_object(
                    self.minio_bucket,
                    fallback_name,
                    data=BytesIO(content),
                    length=len(content),
                    content_type="application/pdf"
                )
                log_info("MinIO", f"Файл загружен с автоматическим именем: {fallback_name}")
                return True
            except Exception as e2:
                log_error("MinIO", f"Не удалось загрузить файл даже с UUID: {e2}")
                return False
        except Exception as e:
            log_error("MinIO", f"Неожиданная ошибка MinIO: {e}")
            return False

    async def _process_item(self, session: aiohttp.ClientSession, item: dict, sem: asyncio.Semaphore) -> bool:
        """Скачивает документ по ссылке из RSS, если его еще нет в БД."""
        url = item['url']
        guid = item['guid']
        pub_date_str = item.get('pubDate', '')
        
        # Попытка распарсить дату из RSS (пример формата: Thu, 01 Sep 2024 10:00:00 +0300)
        pub_date_obj = date.today()
        if pub_date_str:
            try:
                # Отбрасываем таймзону для простоты парсинга
                date_part = " ".join(pub_date_str.split(" ")[:4])
                pub_date_obj = datetime.strptime(date_part, "%a, %d %b %Y").date()
            except Exception:
                pass

        async with sem:
            # Проверка наличия документа в БД
            state_id = None
            async with self.db_session_maker() as db:
                result = await db.execute(select(ExtractState).where(ExtractState.source_url == guid))
                existing_state = result.scalar_one_or_none()
                
                if existing_state:
                    if existing_state.status == "downloaded":
                        return False
                    
                    await db.commit()
                    state_id = existing_state.id

            log_info("CBR Парсер", f"Скачивание документа: {url}")
            download_start_time = datetime.now().time()
            try:
                async with session.get(url, headers=self.headers, ssl=False, timeout=30) as doc_resp:
                    doc_resp.raise_for_status()
                    content = await doc_resp.read()
                    
                    content_disp = doc_resp.headers.get("Content-Disposition", "")
                    filename = ""
                    
                    if content_disp:
                        match_utf8 = re.search(r"filename\*=UTF-8''([^;]+)", content_disp, re.IGNORECASE)
                        if match_utf8:
                            filename = unquote(match_utf8.group(1))
                        else:
                            match_reg = re.search(r'filename="?([^";]+)"?', content_disp, re.IGNORECASE)
                            if match_reg:
                                filename = match_reg.group(1)
                                base_name = filename.rsplit('.', 1)[0]
                                cleaned = base_name.replace("_", "").replace(" ", "").replace("-", "").strip()
                                if not cleaned:
                                    filename = ""
                                    
                    if not filename or filename == "connector" or "%" in filename or "*" in filename:
                        url_name = url.split("/")[-1].split("?")[0]
                        if url_name and url_name.isalnum() and len(url_name) > 3:
                            filename = f"cbr_document_{url_name}.pdf"
                        else:
                            filename = f"cbr_document_{uuid.uuid4().hex[:8]}.pdf"
                    
                    if not filename.endswith(".pdf") and not filename.endswith(".doc") and not filename.endswith(".docx"):
                        filename += ".pdf"
                        
                download_end_time = datetime.now().time()

                # Запускается синхронная загрузка в MinIO в отдельном потоке
                upload_success = await asyncio.to_thread(self.upload_to_minio, content, filename)
                
                if upload_success:
                    # Сохранение в базу данных
                    async with self.db_session_maker() as db:
                        if state_id:
                            # Обновляем существующий
                            result = await db.execute(select(ExtractState).where(ExtractState.id == state_id))
                            state = result.scalar_one()
                            state.file_name = filename
                            state.download_date = date.today()
                            state.download_start_time = download_start_time
                            state.download_end_time = download_end_time
                            state.status = "downloaded"
                        else:
                            # Создаем новый
                            new_state = ExtractState(
                                source="CBR",
                                source_url=guid,
                                file_name=filename,
                                pub_date=pub_date_obj,
                                download_date=date.today(),
                                download_start_time=download_start_time,
                                download_end_time=download_end_time,
                                status="downloaded"
                            )
                            db.add(new_state)
                        await db.commit()
                    return True
                else:
                    # Если upload_to_minio вернул False, отмечаем ошибку (если еще нет стейта, создадим)
                    async with self.db_session_maker() as db:
                        if not state_id:
                            new_state = ExtractState(
                                source="CBR",
                                source_url=guid,
                                file_name=filename,
                                pub_date=pub_date_obj,
                                download_date=date.today(),
                                status="error"
                            )
                            db.add(new_state)
                        else:
                            result = await db.execute(select(ExtractState).where(ExtractState.id == state_id))
                            state = result.scalar_one()
                            state.status = "error"
                        await db.commit()
                return False
            except Exception as e:
                log_error("CBR Парсер", f"Не удалось обработать документ {url}: {e}")
                async with self.db_session_maker() as db:
                    if not state_id:
                        new_state = ExtractState(
                            source="CBR",
                            source_url=guid,
                            file_name="",
                            pub_date=pub_date_obj,
                            download_date=date.today(),
                            status="error"
                        )
                        db.add(new_state)
                    else:
                        result = await db.execute(select(ExtractState).where(ExtractState.id == state_id))
                        state = result.scalar_one()
                        state.status = "error"
                    await db.commit()
                return False

    async def fetch_and_download(self) -> bool:
        """Запускает асинхронный опрос RSS-ленты и загружает новые документы."""
        log_info("CBR Парсер", "Загрузка ленты RSS...")
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(self.rss_url, headers=self.headers, ssl=False, timeout=15) as resp:
                    resp.raise_for_status()
                    xml_text = await resp.text()
            except Exception as e:
                log_error("CBR Парсер", f"Не удалось загрузить RSS ленту: {e}")
                return False
                
            items = []
            try:
                root = ET.fromstring(xml_text)
                for channel in root.findall('channel'):
                    for item in channel.findall('item'):
                        title_elem = item.find('title')
                        link_elem = item.find('link')
                        guid_elem = item.find('guid')
                        pubdate_elem = item.find('pubDate')
                        
                        if title_elem is not None and link_elem is not None and guid_elem is not None:
                            items.append({
                                'title': title_elem.text.strip(),
                                'url': link_elem.text.strip(),
                                'guid': guid_elem.text.strip(),
                                'pubDate': pubdate_elem.text.strip() if pubdate_elem is not None else ""
                            })
            except Exception as e:
                log_error("CBR Парсер", f"Исключение при парсинге XML: {e}")
                return False

            # Вытаскиваем все известные GUID из БД для быстрой фильтрации
            processed_guids = set()
            try:
                async with self.db_session_maker() as db:
                    result = await db.execute(select(ExtractState.source_url).where(ExtractState.source == "CBR"))
                    processed_guids = set(result.scalars().all())
            except Exception as e:
                log_error("CBR Парсер", f"Ошибка получения состояния из БД: {e}")
            
            has_new = False
            new_items = [item for item in items if item['guid'] not in processed_guids]
            
            if not new_items:
                log_info("CBR Парсер", "Нет новых документов в ленте. (пропуск)")
                return False
                
            log_info("CBR Парсер", f"Найдено новых документов: {len(new_items)}")
            
            sem = asyncio.Semaphore(3) # Ограничение: максимум 3 одновременных задачи
            tasks = [self._process_item(session, item, sem) for item in new_items]
            
            results = await asyncio.gather(*tasks)
            if any(results):
                has_new = True

            log_info("CBR Парсер", "Завершена работа парсера CBR RSS.")
            return has_new

async def run_cbr_parser(db_session_maker: Callable[..., AsyncSession], minio_client: Minio, base_dir: str = None) -> bool:
    """Обертка для запуска парсера CBR."""
    if base_dir is None:
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
    parser = CbrRssParser(db_session_maker, minio_client, base_dir)
    return await parser.fetch_and_download()

if __name__ == "__main__":
    from src.core.clients.db import get_async_session_maker
    from src.core.clients.storage import get_minio_client
    
    db_maker = get_async_session_maker()
    minio = get_minio_client()
    asyncio.run(run_cbr_parser(db_maker, minio))
