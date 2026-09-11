import os
import sys
import json
import time
import hashlib
import base64
import asyncio
import io
import re
from datetime import datetime
from pydantic import BaseModel, Field
import fitz
# pyrefly: ignore [missing-import]
from minio.error import S3Error

# Исправление кодировки консоли Windows
sys.stdout.reconfigure(encoding='utf-8')

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from src.core.app_clients import AppClients
from src.utils.document_to_pdf import convert_to_pdf
from src.utils.console_logger import log_info, log_error, log_warning

class DocumentAnalysisResult(BaseModel):
    is_relevant: bool = Field(description="Релевантен ли этот документ для финансового агента?")
    official_name: str = Field(description="Полное точное официальное название на русском языке.")
    system_name: str = Field(description="Системное имя СТРОГО НА АНГЛИЙСКОМ ЯЗЫКЕ (транслит), БЕЗ ТОЧЕК, БЕЗ СЛЕШЕЙ, БЕЗ ПРОБЕЛОВ. Только буквы, цифры и подчеркивания. Формат: tip_dokumenta_nomer_data (например: fz_115_01012024, pismo_03_04_05_18072025).")
    sign_date: str = Field(description="Дата подписания строго в формате дд.мм.гггг.")
    short_number: str = Field(description="Короткий номер акта (например ФЗ-115, 153-И).")

def parse_russian_date(date_str: str) -> datetime:
    """
    Преобразует строку с датой на русском языке в объект datetime.
    """
    if not date_str:
        return datetime.min
    # Проверка формата дд.мм.гггг
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", date_str)
    if match:
        return datetime(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    # Проверка текстового формата (например, 1 января 2024)
    months = {
        "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
        "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
        "янв": 1, "фев": 2, "мар": 3, "апр": 4, "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12
    }
    date_str_lower = date_str.lower()
    for m_name, m_num in months.items():
        if m_name in date_str_lower:
            match = re.search(r"(\d{1,2})\s+" + m_name + r".*?(\d{4})", date_str_lower)
            if match:
                return datetime(int(match.group(2)), m_num, int(match.group(1)))
    return datetime.min

class RepealedDocumentsResult(BaseModel):
    repealed_docs_system_names: list[str] = Field(description="Список системных имен отмененных актов.")

class MainPipeline:
    """
    Главный конвейер (Load Layer).
    Отвечает за выгрузку сырых документов из MinIO, конвертацию, распознавание,
    классификацию с помощью LLM и загрузку в итоговые бакеты.
    """
    def __init__(self):
        self.raw_bucket = "raw-documents"
        self.rag_bucket = "rag-documents"
        
        self.minio_client = AppClients.get_minio_client()
        self.content_ai = AppClients.get_content_ai_client()
        self.cloud_ai = AppClients.get_cloud_ai_client()
        # Ограничение одновременных задач для экономии ресурсов
        self.semaphore = asyncio.Semaphore(1)
        
        # Настройка кэша
        self.cache_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "cache", "main_pipeline")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cache_file = os.path.join(self.cache_dir, "pipeline_cache.json")
        self.cache_lock = asyncio.Lock()
        
        self.llm_model = os.getenv("PIPELINE_MODEL_NAME", "vertex_ai/gemini-3.5-flash")
        self.reasoning_effort = os.getenv("PIPELINE_REASONING_EFFORT", "medium")
        
        # Промпты
        self.prompts_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "prompts"))
        self.system_prompt_path = os.path.join(self.prompts_dir, "document-relevance-system-prompt.md")
        
        self._ensure_buckets()

    def _ensure_buckets(self):
        """Проверяет существование необходимых бакетов и создает их при отсутствии."""
        for bucket in [self.raw_bucket, self.rag_bucket]:
            if not self.minio_client.bucket_exists(bucket):
                self.minio_client.make_bucket(bucket)

    async def _load_cache(self) -> dict:
        """Загружает состояние кэша из файла."""
        async with self.cache_lock:
            if os.path.exists(self.cache_file):
                try:
                    with open(self.cache_file, "r", encoding="utf-8") as f:
                        return json.load(f)
                except json.JSONDecodeError:
                    pass
            return {}

    async def _save_cache(self, file_name: str, cache_data: dict):
        """Сохраняет состояние обработки файла в кэш."""
        async with self.cache_lock:
            current_cache = {}
            if os.path.exists(self.cache_file):
                try:
                    with open(self.cache_file, "r", encoding="utf-8") as f:
                        current_cache = json.load(f)
                except json.JSONDecodeError:
                    pass
            current_cache[file_name] = cache_data
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(current_cache, f, indent=4, ensure_ascii=False)

    async def _read_system_prompt(self) -> str:
        """Читает системный промпт из файла."""
        with open(self.system_prompt_path, "r", encoding="utf-8") as f:
            return f.read()

    def _validate_pdf(self, pdf_bytes: bytes) -> bool:
        """Проверяет валидность PDF файла с помощью библиотеки PyMuPDF (fitz)."""
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            if len(doc) == 0:
                return False
            # Проверка рендеринга первой страницы для убеждения в отсутствии повреждений
            page = doc[0]
            _ = page.get_pixmap()
            doc.close()
            return True
        except Exception:
            return False

    async def _convert_to_pdf_if_needed(self, file_name: str, file_bytes: bytes) -> bytes:
        """Конвертирует документ в формат PDF, если он таковым не является."""
        ext = os.path.splitext(file_name)[1].lower()
        if ext == ".pdf":
            return file_bytes

        log_info("Main Pipeline", f"Конвертация файла {file_name} в PDF...")
        temp_dir = os.path.join(self.cache_dir, "temp_conversion")
        os.makedirs(temp_dir, exist_ok=True)
        
        input_path = os.path.join(temp_dir, file_name)
        with open(input_path, "wb") as f:
            f.write(file_bytes)
            
        try:
            # Вызов синхронной логики конвертации через to_thread
            pdf_path = await asyncio.to_thread(convert_to_pdf, input_path, temp_dir)
            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()
            return pdf_bytes
        finally:
            # Очистка временных файлов
            if os.path.exists(input_path):
                os.remove(input_path)
            expected_pdf = os.path.join(temp_dir, f"{os.path.splitext(file_name)[0]}.pdf")
            if os.path.exists(expected_pdf):
                os.remove(expected_pdf)

    async def process_file(self, file_name: str):
        """
        Обрабатывает отдельный файл:
        1. Проверяет наличие дубликатов по хэшу.
        2. Выполняет конвертацию и распознавание текста.
        3. Запускает анализ документа через LLM (Gemini).
        4. Находит отмененные акты.
        5. Переносит результаты в MinIO и обновляет кэш.
        """
        async with self.semaphore:
            start_time = datetime.now()
            cache_info = {
                "start_time": start_time.isoformat(),
                "status": "processing",
                "file_name": file_name
            }
            try:
                # 1. Скачивание документа
                response = self.minio_client.get_object(self.raw_bucket, file_name)
                file_bytes = response.read()
                response.close()
                response.release_conn()
                
                file_hash = hashlib.md5(file_bytes).hexdigest()
                cache_info["md5"] = file_hash
                
                # Проверка на наличие дубликатов по хэшу
                cache_dict = await self._load_cache()
                for key, data in cache_dict.items():
                    if data.get("status") == "completed" and data.get("md5") == file_hash:
                        log_warning("Main Pipeline", f"Файл {file_name} является дубликатом по MD5 хэшу. Пропуск и удаление.")
                        self.minio_client.remove_object(self.raw_bucket, file_name)
                        return
                
                ext = os.path.splitext(file_name)[1].lower()
                
                if ext == ".md":
                    log_info("Main Pipeline", f"Файл {file_name} уже в формате Markdown. Пропуск Content AI.")
                    markdown_content = file_bytes.decode("utf-8")
                else:
                    # 2. Конвертация в PDF
                    pdf_bytes = await self._convert_to_pdf_if_needed(file_name, file_bytes)
                    
                    # 3. Валидация PDF документа
                    if not self._validate_pdf(pdf_bytes):
                        log_error("Main Pipeline", f"Файл {file_name} является битым PDF. Удаление.")
                        self.minio_client.remove_object(self.raw_bucket, file_name)
                        cache_info["status"] = "failed_validation"
                        await self._save_cache(file_name, cache_info)
                        return
    
                    # 4. Распознавание текста с помощью Content AI
                    log_info("Main Pipeline", f"Отправка {file_name} в Content AI...")
                    temp_pdf_path = os.path.join(self.cache_dir, "temp_recognition.pdf")
                    with open(temp_pdf_path, "wb") as f:
                        f.write(pdf_bytes)
                        
                    # Распознавание может занять время, запуск в отдельном потоке
                    rec_result = await asyncio.to_thread(self.content_ai.recognize, temp_pdf_path)
                    if os.path.exists(temp_pdf_path):
                        os.remove(temp_pdf_path)
                    
                    # Content Capture может вернуть несколько документов, берется первый
                    if not rec_result:
                        log_warning("Main Pipeline", f"Content AI не вернул результатов для {file_name}")
                        cache_info["status"] = "no_recognition_result"
                        self.minio_client.remove_object(self.raw_bucket, file_name)
                        await self._save_cache(file_name, cache_info)
                        return
                        
                    doc_data = list(rec_result.values())[0]
                    markdown_content = doc_data.get("raw_xml", "")
                    
                    if not markdown_content.strip():
                        log_warning("Main Pipeline", f"Content AI вернул пустой текст для {file_name}")
                        cache_info["status"] = "empty_text"
                        self.minio_client.remove_object(self.raw_bucket, file_name)
                        await self._save_cache(file_name, cache_info)
                        return

                # 5. Умный Анализ (Gemini JSON)
                self._log("INFO", f"Анализ документа {file_name} в LLM...")
                sys_prompt = await self._read_system_prompt()
                text_snippet = markdown_content[:5000]
                
                user_prompt1 = (
                    "Проанализируй шапку документа. Определи: "
                    "1) Релевантен ли этот документ для финансового агента? (т.е. является ли документ 'плохим' или 'хорошим') "
                    "2) Полное точное официальное название. "
                    "3) Системное имя (формат типдокумента_номер_датаподписания). "
                    "4) Дату подписания СТРОГО в формате дд.мм.гггг. "
                    "5) Короткий номер акта (например ФЗ-115, 153-И, 1648-У)."
                )
                
                messages = [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": f"Текст:\n{text_snippet}\n\nЗадание: {user_prompt1}"}
                ]
                
                response1 = await self.cloud_ai.acompletion(
                    model=self.llm_model,
                    messages=messages,
                    response_format=DocumentAnalysisResult,
                    reasoning_effort=self.reasoning_effort,
                    temperature=0.0
                )
                
                analysis_json = response1.choices[0].message.content
                analysis_result = json.loads(analysis_json)
                
                messages.append(response1.choices[0].message.model_dump())
                
                is_relevant = analysis_result.get("is_relevant", False)
                system_name = analysis_result.get("system_name", "unknown_doc")
                # Жесткая очистка от слешей и точек, если модель все-таки ошибется
                system_name = system_name.replace("/", "_").replace("\\", "_").replace(".", "").replace(" ", "_")
                
                official_name = analysis_result.get("official_name", "Unknown Document")
                sign_date = analysis_result.get("sign_date", "")
                short_number = analysis_result.get("short_number", "")
                
                cache_info["analysis"] = analysis_result
                cache_info["system_name"] = system_name
                cache_info["official_name"] = official_name
                cache_info["sign_date"] = sign_date
                cache_info["short_number"] = short_number
                
                current_date = parse_russian_date(sign_date)
                base_system_name = re.sub(r"_\d+$", "", system_name) if "_" in system_name else system_name
                
                is_new_version = False
                
                cache_dict = await self._load_cache()
                
                # Точное совпадение system_name
                for key, data in cache_dict.items():
                    if data.get("status") == "success" and data.get("system_name") == system_name:
                        self._log("WARNING", f"Файл {file_name} является полным дубликатом (system_name). Пропуск и удаление.")
                        self.minio_client.remove_object(self.raw_bucket, file_name)
                        return
                        
                # Поиск старых версий
                max_cache_date = datetime.min
                found_prev_version = False
                
                for key, data in cache_dict.items():
                    if data.get("status") == "success":
                        c_system_name = data.get("system_name", "")
                        c_base_system_name = re.sub(r"_\d+$", "", c_system_name) if "_" in c_system_name else c_system_name
                        c_short_number = data.get("short_number", "")
                        
                        if (c_base_system_name == base_system_name) or (short_number and c_short_number == short_number):
                            found_prev_version = True
                            c_date = parse_russian_date(data.get("sign_date", ""))
                            if c_date > max_cache_date:
                                max_cache_date = c_date
                                
                if found_prev_version:
                    if current_date <= max_cache_date:
                        self._log("WARNING", f"Файл {file_name} является старой версией или дубликатом (дата <= {max_cache_date.strftime('%d.%m.%Y') if max_cache_date != datetime.min else 'N/A'}). Пропуск и удаление.")
                        self.minio_client.remove_object(self.raw_bucket, file_name)
                        return
                    else:
                        self._log("INFO", f"Файл {file_name} является НОВОЙ редакцией. Обработка продолжается.")
                        is_new_version = True

                
                
                # 6. Поиск отмененных актов
                repeal_keywords = [r"утративш.*? силу", r"отменить", r"признать недействительн.*?"]
                repeal_pattern = re.compile("|".join(repeal_keywords), re.IGNORECASE)
                
                repeal_snippets = []
                for match in repeal_pattern.finditer(markdown_content):
                    start = max(0, match.start() - 2000)
                    end = min(len(markdown_content), match.end() + 2000)
                    repeal_snippets.append(markdown_content[start:end])
                
                repealed_docs = []
                if repeal_snippets:
                    log_info("Main Pipeline", f"Поиск отмененных актов в {file_name}...")
                    snippets_text = "\n\n---\n\n".join(repeal_snippets)
                    user_prompt2 = (
                        "В данном документе упоминаются возможные отмены правовых актов. "
                        "Определи, какие уже существующие документы отменяет этот обрабатываемый документ. "
                        "Верни строго JSON список номеров всех актов (например ФЗ-115, ), которые текущий документ отменяет. "
                        "Если отмененных актов нет, верни пустой список.\n\n"
                        f"Фрагменты для анализа:\n{snippets_text}"
                    )
                    messages.append({"role": "user", "content": user_prompt2})
                    
                    response2 = await self.cloud_ai.acompletion(
                        model=self.llm_model,
                        messages=messages,
                        response_format=RepealedDocumentsResult,
                        reasoning_effort=self.reasoning_effort,
                        temperature=0.0
                    )
                    
                    repeal_json = response2.choices[0].message.content
                    repeal_result = json.loads(repeal_json)
                    repealed_docs = list(set(repeal_result.get("repealed_docs_system_names", [])))
                    
                if is_new_version and short_number:
                    repealed_docs.append(short_number)
                    repealed_docs = list(set(repealed_docs))
                    log_info("Main Pipeline", f"В список отмен добавлена предыдущая версия: {short_number}")
                    
                cache_info["repealed_docs"] = repealed_docs
                
                # 7. Маршрутизация в rag-documents
                if is_relevant:
                    log_info("Main Pipeline", f"Документ {file_name} признан релевантным. Сохранение в upsert...")
                    md_bytes = markdown_content.encode("utf-8")
                    metadata = {
                        "official-name": base64.b64encode(official_name.encode('utf-8')).decode('ascii'),
                        "sign-date": sign_date,
                        "short-number": base64.b64encode(short_number.encode('utf-8')).decode('ascii') if short_number else "unknown"
                    }
                    self.minio_client.put_object(
                        self.rag_bucket,
                        f"upsert/{system_name}.md",
                        io.BytesIO(md_bytes),
                        length=len(md_bytes),
                        metadata=metadata,
                        content_type="text/markdown"
                    )
                    
                if repealed_docs:
                    log_info("Main Pipeline", f"В {file_name} найдены отмененные акты: {len(repealed_docs)} шт. Сохранение в delete...")
                    del_data = json.dumps(repealed_docs, ensure_ascii=False).encode("utf-8")
                    self.minio_client.put_object(
                        self.rag_bucket,
                        f"delete/event_repeal_{system_name}.json",
                        io.BytesIO(del_data),
                        length=len(del_data),
                        content_type="application/json"
                    )

                # 8. Удаление оригинала
                self.minio_client.remove_object(self.raw_bucket, file_name)
                
                cache_info["status"] = "completed"
                cache_info["end_time"] = datetime.now().isoformat()
                await self._save_cache(file_name, cache_info)
                log_info("Main Pipeline", f"Успешно обработан {file_name}")

            except Exception as e:
                log_error("Main Pipeline", f"Ошибка при обработке {file_name}: {str(e)}")
                cache_info["status"] = "error"
                cache_info["error"] = str(e)
                cache_info["end_time"] = datetime.now().isoformat()
                await self._save_cache(file_name, cache_info)

    async def run(self):
        """
        Запускает цикл обработки для всех документов в бакете сырых файлов.
        """
        log_info("Main Pipeline", "Запуск Main Pipeline Load Layer...")
        objects = list(self.minio_client.list_objects(self.raw_bucket, recursive=True))
        
        if not objects:
            log_info("Main Pipeline", "Очередь raw-documents пуста.")
            return

        log_info("Main Pipeline", f"Найдено {len(objects)} файлов для обработки.")
        
        tasks = []
        for obj in objects:
            tasks.append(self.process_file(obj.object_name))
            
        await asyncio.gather(*tasks)
        log_info("Main Pipeline", "Пайплайн завершил работу.")

def start_pipeline():
    pipeline = MainPipeline()
    asyncio.run(pipeline.run())

if __name__ == "__main__":
    start_pipeline()
