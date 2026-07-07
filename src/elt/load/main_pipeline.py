import os
import sys
import json
import time
import asyncio
import io
import re
from datetime import datetime
from pydantic import BaseModel, Field
import fitz
import colorama
from colorama import Fore, Style
# pyrefly: ignore [missing-import]
from minio.error import S3Error

# Fix Windows console encoding
sys.stdout.reconfigure(encoding='utf-8')
colorama.init()

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from src.core.app_clients import AppClients
from src.utils.document_to_pdf import convert_to_pdf

class DocumentAnalysisResult(BaseModel):
    is_relevant: bool = Field(description="Релевантен ли этот документ для финансового агента?")
    official_name: str = Field(description="Полное точное официальное название.")
    system_name: str = Field(description="Системное имя (формат типдокумента_номер_датаподписания).")
    sign_date: str = Field(description="Дата подписания (дд.мм.гггг).")

class RepealedDocumentsResult(BaseModel):
    repealed_docs_system_names: list[str] = Field(description="Список системных имен отмененных актов.")

class MainPipeline:
    def __init__(self):
        self.raw_bucket = "raw-documents"
        self.rag_bucket = "rag-documents"
        
        self.minio_client = AppClients.get_minio_client()
        self.content_ai = AppClients.get_content_ai_client()
        self.cloud_ai = AppClients.get_cloud_ai_client()
        
        self.semaphore = asyncio.Semaphore(3)
        
        # Cache setup
        self.cache_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "cache", "main_pipeline")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cache_file = os.path.join(self.cache_dir, "pipeline_cache.json")
        self.cache_lock = asyncio.Lock()
        
        self.llm_model = os.getenv("PIPELINE_MODEL_NAME", "vertex_ai/gemini-3.5-flash")
        self.reasoning_effort = os.getenv("PIPELINE_REASONING_EFFORT", "medium")
        
        # Prompts
        self.prompts_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "prompts"))
        self.system_prompt_path = os.path.join(self.prompts_dir, "document-relevance-system-prompt.md")
        
        self._ensure_buckets()

    def _ensure_buckets(self):
        for bucket in [self.raw_bucket, self.rag_bucket]:
            if not self.minio_client.bucket_exists(bucket):
                self.minio_client.make_bucket(bucket)

    async def _load_cache(self):
        async with self.cache_lock:
            if os.path.exists(self.cache_file):
                try:
                    with open(self.cache_file, "r", encoding="utf-8") as f:
                        return json.load(f)
                except json.JSONDecodeError:
                    pass
            return {}

    async def _save_cache(self, file_name: str, cache_data: dict):
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

    def _log(self, level: str, msg: str):
        color = Fore.WHITE
        if level == "INFO": color = Fore.CYAN
        elif level == "SUCCESS": color = Fore.GREEN
        elif level == "WARNING": color = Fore.YELLOW
        elif level == "ERROR": color = Fore.RED
        print(f"{color}[{level}] {msg}{Style.RESET_ALL}")

    async def _read_system_prompt(self) -> str:
        with open(self.system_prompt_path, "r", encoding="utf-8") as f:
            return f.read()

    def _validate_pdf(self, pdf_bytes: bytes) -> bool:
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            if len(doc) == 0:
                return False
            # Check rendering on the first page to ensure it's not corrupt
            page = doc[0]
            _ = page.get_pixmap()
            doc.close()
            return True
        except Exception:
            return False

    async def _convert_to_pdf_if_needed(self, file_name: str, file_bytes: bytes) -> bytes:
        ext = os.path.splitext(file_name)[1].lower()
        if ext == ".pdf":
            return file_bytes

        self._log("INFO", f"Конвертация файла {file_name} в PDF...")
        temp_dir = os.path.join(self.cache_dir, "temp_conversion")
        os.makedirs(temp_dir, exist_ok=True)
        
        input_path = os.path.join(temp_dir, file_name)
        with open(input_path, "wb") as f:
            f.write(file_bytes)
            
        try:
            # We call synchronously blocking sub-process logic via to_thread
            pdf_path = await asyncio.to_thread(convert_to_pdf, input_path, temp_dir)
            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()
            return pdf_bytes
        finally:
            # Clean up
            if os.path.exists(input_path):
                os.remove(input_path)
            expected_pdf = os.path.join(temp_dir, f"{os.path.splitext(file_name)[0]}.pdf")
            if os.path.exists(expected_pdf):
                os.remove(expected_pdf)

    async def process_file(self, file_name: str):
        async with self.semaphore:
            start_time = datetime.now()
            cache_info = {
                "start_time": start_time.isoformat(),
                "status": "processing",
                "file_name": file_name
            }
            try:
                # 1. Скачивание
                response = self.minio_client.get_object(self.raw_bucket, file_name)
                file_bytes = response.read()
                response.close()
                response.release_conn()
                
                # 2. Конвертация в PDF
                pdf_bytes = await self._convert_to_pdf_if_needed(file_name, file_bytes)
                
                # 3. Валидация PDF
                if not self._validate_pdf(pdf_bytes):
                    self._log("ERROR", f"Файл {file_name} является битым PDF. Удаление.")
                    self.minio_client.remove_object(self.raw_bucket, file_name)
                    cache_info["status"] = "failed_validation"
                    await self._save_cache(file_name, cache_info)
                    return

                # 4. Content AI (Распознавание)
                self._log("INFO", f"Отправка {file_name} в Content AI...")
                temp_pdf_path = os.path.join(self.cache_dir, "temp_recognition.pdf")
                with open(temp_pdf_path, "wb") as f:
                    f.write(pdf_bytes)
                    
                # Content AI can take time, run in thread
                rec_result = await asyncio.to_thread(self.content_ai.recognize, temp_pdf_path)
                if os.path.exists(temp_pdf_path):
                    os.remove(temp_pdf_path)
                
                # Since Content Capture might return multiple documents, we take the first available
                if not rec_result:
                    self._log("WARNING", f"Content AI не вернул результатов для {file_name}")
                    cache_info["status"] = "no_recognition_result"
                    self.minio_client.remove_object(self.raw_bucket, file_name)
                    await self._save_cache(file_name, cache_info)
                    return
                    
                doc_data = list(rec_result.values())[0]
                markdown_content = doc_data.get("raw_xml", "")
                
                if not markdown_content.strip():
                    self._log("WARNING", f"Content AI вернул пустой текст для {file_name}")
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
                    "4) Дату подписания."
                )
                
                messages = [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": f"Текст:\n{text_snippet}\n\nЗадание: {user_prompt1}"}
                ]
                
                response1 = await self.cloud_ai.acompletion(
                    model=self.llm_model,
                    messages=messages,
                    response_format=DocumentAnalysisResult,
                    reasoning_effort=self.reasoning_effort
                )
                
                analysis_json = response1.choices[0].message.content
                analysis_result = json.loads(analysis_json)
                
                messages.append(response1.choices[0].message.model_dump())
                
                is_relevant = analysis_result.get("is_relevant", False)
                system_name = analysis_result.get("system_name", "unknown_doc")
                official_name = analysis_result.get("official_name", "Unknown Document")
                sign_date = analysis_result.get("sign_date", "")
                
                cache_info["analysis"] = analysis_result
                
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
                    self._log("INFO", f"Поиск отмененных актов в {file_name}...")
                    snippets_text = "\n\n---\n\n".join(repeal_snippets)
                    user_prompt2 = (
                        "В данном документе упоминаются возможные отмены правовых актов. "
                        "Определи, какие уже существующие документы отменяет этот обрабатываемый документ. "
                        "Верни строго JSON список всех актов, которые текущий документ отменяет. "
                        "Если отмененных актов нет, верни пустой список.\n\n"
                        f"Фрагменты для анализа:\n{snippets_text}"
                    )
                    messages.append({"role": "user", "content": user_prompt2})
                    
                    response2 = await self.cloud_ai.acompletion(
                        model=self.llm_model,
                        messages=messages,
                        response_format=RepealedDocumentsResult,
                        reasoning_effort=self.reasoning_effort
                    )
                    
                    repeal_json = response2.choices[0].message.content
                    repeal_result = json.loads(repeal_json)
                    repealed_docs = list(set(repeal_result.get("repealed_docs_system_names", [])))
                    
                cache_info["repealed_docs"] = repealed_docs
                
                # 7. Маршрутизация в rag-documents
                if is_relevant:
                    self._log("SUCCESS", f"Документ {file_name} признан релевантным. Сохранение в upsert...")
                    md_bytes = markdown_content.encode("utf-8")
                    metadata = {
                        "official-name": official_name.encode('utf-8').decode('latin-1', 'ignore'),
                        "sign-date": sign_date
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
                    self._log("SUCCESS", f"В {file_name} найдены отмененные акты: {len(repealed_docs)} шт. Сохранение в delete...")
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
                self._log("SUCCESS", f"Успешно обработан {file_name}")

            except Exception as e:
                self._log("ERROR", f"Ошибка при обработке {file_name}: {str(e)}")
                cache_info["status"] = "error"
                cache_info["error"] = str(e)
                cache_info["end_time"] = datetime.now().isoformat()
                await self._save_cache(file_name, cache_info)

    async def run(self):
        self._log("INFO", "Запуск Main Pipeline Load Layer...")
        objects = list(self.minio_client.list_objects(self.raw_bucket, recursive=True))
        
        if not objects:
            self._log("INFO", "Очередь raw-documents пуста.")
            return

        self._log("INFO", f"Найдено {len(objects)} файлов для обработки.")
        
        tasks = []
        for obj in objects:
            tasks.append(self.process_file(obj.object_name))
            
        await asyncio.gather(*tasks)
        self._log("SUCCESS", "Пайплайн завершил работу.")

def start_pipeline():
    pipeline = MainPipeline()
    asyncio.run(pipeline.run())

if __name__ == "__main__":
    start_pipeline()
