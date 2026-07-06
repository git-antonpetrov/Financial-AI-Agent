import os
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.stdout.reconfigure(encoding='utf-8')

import re
import json
import hashlib
import shutil
import asyncio
import glob
from datetime import datetime
from dotenv import load_dotenv

# pyrefly: ignore [missing-import]
from litellm import acompletion
from src.services.document_to_pdf import convert_to_pdf
from src.services.cloud_text_extractor import extract_text_cloud

class StorageManager:
    def __init__(self):
        load_dotenv()
        self.landing_dir = "data/1_landing"
        self.storage_dir = "data/2_storage"
        self.tracker_file = "data/state_tracker.json"
        
        os.makedirs(self.landing_dir, exist_ok=True)
        os.makedirs(self.storage_dir, exist_ok=True)
        
        if not os.path.exists(self.tracker_file):
            with open(self.tracker_file, "w", encoding="utf-8") as f:
                json.dump({}, f)
                
        self.llm_model = os.getenv("FILE_NAME_MODEL_NAME", "vertex_ai/gemini-3.5-flash")
        self.vertex_project = os.getenv("VERTEX_PROJECT")
        self.vertex_location = os.getenv("VERTEX_LOCATION")
        self.reasoning_effort = os.getenv("FILE_NAME_REASONING_EFFORT", "medium")
        
        # Limit LLM concurrency to 5 to avoid overwhelming litellm retries for the 15 RPM limit
        self.api_semaphore = asyncio.Semaphore(5)
        self.io_semaphore = asyncio.Semaphore(4)
        self.tracker_lock = asyncio.Lock()

    def _calculate_md5(self, filepath: str) -> str:
        hasher = hashlib.md5()
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def _load_tracker(self) -> dict:
        with open(self.tracker_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_tracker(self, tracker_data: dict):
        with open(self.tracker_file, "w", encoding="utf-8") as f:
            json.dump(tracker_data, f, indent=4, ensure_ascii=False)

    def _sanitize_filename(self, raw_name: str) -> str:
        name = raw_name.lower().replace(" ", "_").replace("-", "_")
        name = re.sub(r'[^a-z0-9_.]', '', name)
        
        if not name.endswith(".pdf"):
            name += ".pdf"
            
        # Ensure it has the _DDMMYYYY format before .pdf
        if not re.search(r'_\d{8}\.pdf$', name):
            name = name.replace(".pdf", "_00000000.pdf")
            
        return name
        
    def _parse_date_from_filename(self, filename: str) -> int:
        """Extracts DDMMYYYY from filename and returns YYYYMMDD as an integer for easy comparison."""
        match = re.search(r'_(\d{2})(\d{2})(\d{4})\.pdf$', filename)
        if match:
            dd, mm, yyyy = match.groups()
            return int(f"{yyyy}{mm}{dd}")
        return 0

    async def _determine_system_name(self, filepath: str) -> str:
        async with self.api_semaphore:
            text = await extract_text_cloud(filepath, num_pages=2)
            
        if not text:
            print(f"\033[91m[Брак]\033[0m Не удалось извлечь текст из {os.path.basename(filepath)}.")
            return None

        prompt = (
            f"Текст документа (первые две страницы):\n{text[:4000]}\n\n"
            "Сгенерируй короткое имя файла на транслите/английском. "
            "ОБЯЗАТЕЛЬНО включи ТИП и НОМЕР документа (указание, положение, ФЗ и т.д.). "
            "Найди дату документа. Формат строго: тип_номер_DDMMYYYY.pdf. "
            "Пример: ukazanie_6018_u_22042024.pdf или polozhenie_382p_01012020.pdf. "
            "Если даты нет, используй 00000000. Никаких других слов, только имя файла."
        )
        
        try:
            async with self.api_semaphore:
                # Pace requests to avoid hitting the 15 RPM limit instantly
                await asyncio.sleep(3) 
                response = await asyncio.wait_for(
                    acompletion(
                        model=self.llm_model,
                        messages=[{"role": "user", "content": prompt}],
                        vertex_project=self.vertex_project,
                        vertex_location=self.vertex_location,
                        reasoning_effort=self.reasoning_effort
                    ),
                    timeout=30.0
                )
            llm_name = response.choices[0].message.content.strip()
            return self._sanitize_filename(llm_name)
        except Exception as e:
            print(f"\033[93m[Предупреждение]\033[0m Ошибка LLM: {e}")
            return None

    def _log_deletion(self, reason: str, filepath: str, replaced_by: str = None):
        cache_dir = os.path.join("data", ".storage_manager_cache")
        os.makedirs(cache_dir, exist_ok=True)
        log_path = os.path.join(cache_dir, "deletion_log.jsonl")
        import json
        from datetime import datetime
        entry = {
            "timestamp": datetime.now().isoformat(),
            "reason": reason,
            "filepath": filepath,
            "replaced_by": replaced_by
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def process_file(self, landing_filename: str) -> bool:
        if not os.path.exists(landing_filename):
            return False
            
        base_ext = os.path.splitext(landing_filename)[1].lower()
        
        # 1. Convert to PDF if needed
        pdf_filepath = landing_filename
        if base_ext != '.pdf':
            print(f"\033[96m[Конвертация]\033[0m {landing_filename} -> PDF")
            try:
                async with self.io_semaphore:
                    pdf_filepath = await asyncio.to_thread(convert_to_pdf, landing_filename, self.landing_dir)
                if pdf_filepath != landing_filename:
                    os.remove(landing_filename)
            except Exception as e:
                print(f"\033[91m[Ошибка конвертации]\033[0m {landing_filename}: {e}")
                return False

        # 2. Check MD5 globally (Глухой фильтр)
        md5_hash = await asyncio.to_thread(self._calculate_md5, pdf_filepath)
        
        async with self.tracker_lock:
            tracker_data = self._load_tracker()
            
            if md5_hash in tracker_data.values():
                print(f"\033[93m[Skip]\033[0m Точный дубликат обнаружен по MD5 хешу. Удаляем {pdf_filepath}")
                os.remove(pdf_filepath)
                return False

        # 3. LLM Naming with Date
        new_filename = await self._determine_system_name(pdf_filepath)
        if not new_filename:
            print(f"\033[93m[Пропуск]\033[0m {pdf_filepath} не распознан или ошибка LLM. Оставляем в 1_landing для повторной попытки.")
            return False

        # 4. Resolve name collisions (Overwrite protection)
        base_name_match = re.search(r'^(.*?)_\d{8}\.pdf$', new_filename)
        if not base_name_match:
            base_name_without_date = new_filename.replace(".pdf", "")
        else:
            base_name_without_date = base_name_match.group(1)
            
        new_date_val = self._parse_date_from_filename(new_filename)
        
        existing_files = glob.glob(os.path.join(self.storage_dir, f"{base_name_without_date}*.pdf"))
        
        should_move = True
        
        async with self.tracker_lock:
            tracker_data = self._load_tracker() # Re-load under lock before mutating
            
            for existing_path in existing_files:
                existing_name = os.path.basename(existing_path)
                if re.search(rf'^{re.escape(base_name_without_date)}_\d{{8}}\.pdf$', existing_name):
                    existing_date_val = self._parse_date_from_filename(existing_name)
                    
                    if new_date_val > existing_date_val:
                        print(f"\033[94m[Обновление]\033[0m Найдена новая версия! Удаляем старую: {existing_name}")
                        self._log_deletion("overwritten_by_newer_version", existing_path, new_filename)
                        os.remove(existing_path)
                        if existing_name in tracker_data:
                            del tracker_data[existing_name]
                    else:
                        print(f"\033[93m[Устарело]\033[0m Версия {new_filename} не новее {existing_name}. Игнорируем.")
                        should_move = False
                        break 
    
            if should_move:
                target_path = os.path.join(self.storage_dir, new_filename)
                print(f"\033[92m[Move]\033[0m Перемещение в хранилище: {new_filename}")
                shutil.move(pdf_filepath, target_path)
                tracker_data[new_filename] = md5_hash
                self._save_tracker(tracker_data)
                return True
            else:
                self._log_deletion("skipped_older_version", pdf_filepath, existing_name)
                os.remove(pdf_filepath)
                return False

    async def process_all_landing_files(self):
        from filelock import FileLock, Timeout
        lock_path = "data/.storage_manager.lock"
        lock = FileLock(lock_path, timeout=0)
        
        try:
            with lock:
                files = glob.glob(os.path.join(self.landing_dir, "*"))
                files = [f for f in files if os.path.isfile(f) and not f.endswith(".gitkeep")]
                
                if not files:
                    print(f"\033[93m[Инфо]\033[0m Папка {self.landing_dir} пуста.")
                    return

                print(f"\033[96m[Старт]\033[0m Обработка {len(files)} файлов в хранилище...")
                
                # Limit overall concurrency to avoid starving the ThreadPoolExecutor with MD5 tasks
                global_semaphore = asyncio.Semaphore(20)
                
                async def bounded_process_file(filepath):
                    async with global_semaphore:
                        return await self.process_file(filepath)
                
                tasks = [bounded_process_file(filepath) for filepath in files]
                await asyncio.gather(*tasks)
                    
                print("\033[92m[Готово]\033[0m Все файлы обработаны.")
        except Timeout:
            print(f"\033[93m[Skip]\033[0m StorageManager уже запущен другим процессом. Завершение...")
            return

if __name__ == "__main__":
    manager = StorageManager()
    asyncio.run(manager.process_all_landing_files())
