import os
import sys
import json
import uuid
import urllib3
import re
import asyncio
import aiohttp
from io import BytesIO
from urllib.parse import unquote
import xml.etree.ElementTree as ET
from src.utils.console_logger import log_info, log_warning, log_error

# Исправление кодировки консоли Windows
sys.stdout.reconfigure(encoding='utf-8')

try:
    # pyrefly: ignore [missing-import]
    from src.core.app_clients import AppClients
    # pyrefly: ignore [missing-import]
    from minio.error import S3Error
except ImportError:
    log_error("CBR Парсер", "Библиотека 'minio' не найдена. Установите ее: pip install minio")
    sys.exit(1)

# Добавление корня проекта в sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class CbrRssParser:
    """
    Парсер для загрузки документов из RSS-ленты Центрального Банка РФ.
    Скачивает файлы и сохраняет их в бакет MinIO.
    """
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.cache_dir = os.path.join(base_dir, "data", "cache", "cbr_rss_parser")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.tracker_path = os.path.join(self.cache_dir, "cbr_state_tracker.json")
        
        self.rss_url = "https://www.cbr.ru/rss/navr"
        
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7'
        }
        
        self.minio_bucket = "raw-documents"
        self.minio_client = AppClients.get_minio_client()
        
        self._ensure_bucket_exists()

    def _ensure_bucket_exists(self):
        try:
            if not self.minio_client.bucket_exists(self.minio_bucket):
                self.minio_client.make_bucket(self.minio_bucket)
                log_info("CBR Парсер", f"Создан бакет: {self.minio_bucket}")
        except Exception as e:
            log_error("MinIO", f"Не удалось проверить или создать бакет: {e}")

    def _load_tracker(self) -> dict:
        if os.path.exists(self.tracker_path):
            try:
                with open(self.tracker_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return {"processed_guids": data}
                    return data
            except json.JSONDecodeError:
                pass
        return {"processed_guids": []}

    def _save_tracker(self, data: dict):
        with open(self.tracker_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def upload_to_minio(self, content: bytes, original_filename: str) -> bool:
        """Синхронная функция загрузки в MinIO. Должна вызываться через asyncio.to_thread."""
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

    async def _process_item(self, session, item, sem, tracker, processed_guids):
        url = item['url']
        async with sem:
            log_info("CBR Парсер", f"Скачивание документа: {url}")
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

                    # Запускается синхронная загрузка в MinIO в отдельном потоке
                    upload_success = await asyncio.to_thread(self.upload_to_minio, content, filename)
                    
                    if upload_success:
                        processed_guids.add(item['guid'])
                        tracker["processed_guids"] = list(processed_guids)
                        # Синхронная запись в файл
                        self._save_tracker(tracker)
                        return True
                    return False
            except Exception as e:
                log_error("CBR Парсер", f"Не удалось обработать документ {url}: {e}")
                return False

    async def fetch_and_download(self) -> bool:
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
                        
                        if title_elem is not None and link_elem is not None and guid_elem is not None:
                            items.append({
                                'title': title_elem.text.strip(),
                                'url': link_elem.text.strip(),
                                'guid': guid_elem.text.strip()
                            })
            except Exception as e:
                log_error("CBR Парсер", f"Исключение при парсинге XML: {e}")
                return False

            tracker = self._load_tracker()
            processed_guids = set(tracker.get("processed_guids", []))
            
            has_new = False
            new_items = [item for item in items if item['guid'] not in processed_guids]
            
            if not new_items:
                log_info("CBR Парсер", "Нет новых документов в ленте. (пропуск)")
                return False
                
            log_info("CBR Парсер", f"Найдено новых документов: {len(new_items)}")
            
            sem = asyncio.Semaphore(3) # Ограничение: максимум 3 одновременных задачи
            tasks = [self._process_item(session, item, sem, tracker, processed_guids) for item in new_items]
            
            results = await asyncio.gather(*tasks)
            if any(results):
                has_new = True

            log_info("CBR Парсер", "Завершена работа парсера CBR RSS.")
            return has_new

def run_cbr_parser(base_dir=None):
    if base_dir is None:
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
    parser = CbrRssParser(base_dir)
    return asyncio.run(parser.fetch_and_download())

if __name__ == "__main__":
    run_cbr_parser()
