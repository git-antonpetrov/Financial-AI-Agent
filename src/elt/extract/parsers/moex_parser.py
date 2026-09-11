import os
import sys
import json
import uuid
import urllib3
import re
import asyncio
import aiohttp
from io import BytesIO
from urllib.parse import urljoin, unquote
# pyrefly: ignore [missing-import]
from bs4 import BeautifulSoup
from src.utils.console_logger import log_info, log_error, log_warning

# Исправление кодировки консоли Windows
sys.stdout.reconfigure(encoding='utf-8')

try:
    # pyrefly: ignore [missing-import]
    from src.core.app_clients import AppClients
    # pyrefly: ignore [missing-import]
    from minio.error import S3Error
except ImportError:
    log_error("MOEX Парсер", "Библиотека 'minio' не найдена. Установите ее: pip install minio")
    sys.exit(1)

# Добавление корня проекта в sys.path для импортов из src
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

# Отключение предупреждений InsecureRequestWarning для ГОСТ-сайтов
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class MoexParser:
    """
    Парсер для загрузки документов с сайта Московской Биржи (MOEX).
    Скачивает файлы и сохраняет их в бакет MinIO.
    """
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.cache_dir = os.path.join(base_dir, "data", "cache", "moex_parser")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.tracker_path = os.path.join(self.cache_dir, "moex_state_tracker.json")
        
        self.target_urls = [
            "https://www.moex.com/ru/documents/301",
            "https://www.nationalclearingcentre.ru/catalog/020415/76",
            "https://www.nationalclearingcentre.ru/catalog/020415/9374",
            "https://www.nationalclearingcentre.ru/catalog/020415/93",
            "https://www.moex.com/ru/documents/257"
        ]
        
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
                log_info("MOEX Парсер", f"Создан бакет: {self.minio_bucket}")
        except Exception as e:
            log_error("MinIO", f"Не удалось проверить или создать бакет: {e}")

    def _load_tracker(self) -> dict:
        if os.path.exists(self.tracker_path):
            try:
                with open(self.tracker_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except json.JSONDecodeError:
                pass
        return {"processed_urls": []}

    def _save_tracker(self, data: dict):
        with open(self.tracker_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def _extract_document_link(self, soup: BeautifulSoup, base_url: str) -> str:
        """Извлекает ссылку на документ 'Действующая редакция' со страницы."""
        if 'moex.com' in base_url:
            for link in soup.find_all("a", href=True):
                text = link.text.lower()
                if "правила" in text and "изменения" not in text:
                    return link["href"]
                        
        if 'nationalclearingcentre.ru' in base_url:
            for link in soup.find_all("a", href=True):
                text = link.text.lower()
                if "правила" in text and "изменения" not in text and "connector?cmd=file" in link["href"]:
                    return link["href"]
        return None

    def upload_to_minio(self, content: bytes, original_filename: str) -> bool:
        """Загружает файл в MinIO. Синхронная функция, вызывается через to_thread."""
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
                fallback_name = f"moex_doc_{uuid.uuid4().hex}.pdf"
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

    async def _process_page(self, session, page_url, sem, tracker, processed_urls):
        async with sem:
            try:
                async with session.get(page_url, headers=self.headers, ssl=False, timeout=15) as resp:
                    resp.raise_for_status()
                    html_text = await resp.text()
                
                # Парсинг BeautifulSoup выполняется в потоке, чтобы не блокировать event loop
                soup = await asyncio.to_thread(BeautifulSoup, html_text, 'html.parser')
                pdf_path = self._extract_document_link(soup, page_url)
                
                if not pdf_path:
                    log_warning("MOEX Парсер", f"Не удалось найти документ на странице: {page_url}")
                    return False
                
                if pdf_path.startswith('/'):
                    domain = "https://www.moex.com" if "moex.com" in page_url else "https://www.nationalclearingcentre.ru"
                    absolute_url = domain + pdf_path
                else:
                    absolute_url = urljoin(page_url, pdf_path)
                
                if absolute_url in processed_urls:
                    log_info("MOEX Парсер", f"Документ уже загружен ранее (пропуск): {absolute_url}")
                    return False
                
                log_info("MOEX Парсер", f"Скачивание нового документа: {absolute_url}")
                async with session.get(absolute_url, headers=self.headers, ssl=False, timeout=30) as doc_resp:
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
                    url_name = pdf_path.split("/")[-1].split("?")[0]
                    if url_name and url_name != "connector" and len(url_name) > 3:
                        filename = unquote(url_name)
                    else:
                        filename = f"moex_document_{uuid.uuid4().hex[:8]}.pdf"
                
                if not filename.endswith(".pdf") and not filename.endswith(".doc") and not filename.endswith(".docx"):
                    filename += ".pdf"

                upload_success = await asyncio.to_thread(self.upload_to_minio, content, filename)
                
                if upload_success:
                    processed_urls.add(absolute_url)
                    tracker["processed_urls"] = list(processed_urls)
                    self._save_tracker(tracker)
                    return True
                return False
                
            except Exception as e:
                log_error("MOEX Парсер", f"Не удалось обработать страницу {page_url}: {e}")
                return False

    async def fetch_and_download(self) -> bool:
        log_info("MOEX Парсер", f"Старт параллельной проверки документов ({len(self.target_urls)} страниц)...")
        tracker = self._load_tracker()
        processed_urls = set(tracker.get("processed_urls", []))
        
        sem = asyncio.Semaphore(3)
        
        async with aiohttp.ClientSession() as session:
            tasks = [self._process_page(session, url, sem, tracker, processed_urls) for url in self.target_urls]
            results = await asyncio.gather(*tasks)
            
        log_info("MOEX Парсер", "Завершена работа парсера MOEX.")
        return any(results)

def run_moex_parser(base_dir=None):
    if base_dir is None:
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
    parser = MoexParser(base_dir)
    return asyncio.run(parser.fetch_and_download())

if __name__ == "__main__":
    run_moex_parser()
