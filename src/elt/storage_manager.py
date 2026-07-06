import os
import re
import json
import hashlib
import shutil
import asyncio
from dotenv import load_dotenv
# pyrefly: ignore [missing-import]
from src.utils.text_extractor import UniversalExtractor
from litellm import acompletion

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
                
        self.llm_model = os.getenv("META_MODEL_NAME", "vertex_ai/gemini-3.5-flash")
        self.vertex_project = os.getenv("VERTEX_PROJECT")
        self.vertex_location = os.getenv("VERTEX_LOCATION")
        self.reasoning_effort = os.getenv("META_REASONING_EFFORT", "medium")

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

    def _sanitize_filename(self, raw_name: str, extension: str) -> str:
        # Переводим в нижний регистр, заменяем пробелы и тире на подчеркивания
        name = raw_name.lower().replace(" ", "_").replace("-", "_")
        # Удаляем все, кроме латинских букв, цифр и _
        name = re.sub(r'[^a-z0-9_]', '', name)
        
        if not name:
            name = "unknown"
            
        if not extension.startswith("."):
            extension = "." + extension
            
        return name + extension

    async def _extract_first_page_text(self, filepath: str) -> str:
        text = await UniversalExtractor.extract_text(filepath, max_pages=1)
        return text[:1000] if text else ""

    async def _determine_system_name(self, filepath: str, rss_title: str = None) -> str:
        extension = os.path.splitext(filepath)[1].lower()
        
        # Ступень 1: RSS
        if rss_title:
            match = re.search(r'(фз|указание|положение|инструкция)\s*[-№]?\s*(\d+[а-яА-Яa-zA-Z0-9\-]*)', rss_title, re.IGNORECASE)
            if match:
                print(f"\033[94m[Regex-RSS]\033[0m Найдено совпадение в RSS: {match.group(0)}")
                return self._sanitize_filename(f"{match.group(1)}_{match.group(2)}", extension)

        # Ступень 2: Regex
        text = await self._extract_first_page_text(filepath)
        if text:
            # Ищем 123-ФЗ, 123-У, 123-П, 123-И
            match = re.search(r'(\d{1,5})[-]([УПИФЗ]{1,2})', text, re.IGNORECASE)
            if match:
                type_map = {'ФЗ': 'fz', 'У': 'ukazanie', 'П': 'polozhenie', 'И': 'instrukciya'}
                doc_type_ru = match.group(2).upper()
                doc_type_en = type_map.get(doc_type_ru, doc_type_ru.lower())
                
                print(f"\033[94m[Regex]\033[0m Найдено совпадение в тексте: {match.group(0)}")
                return self._sanitize_filename(f"{doc_type_en}_{match.group(1)}", extension)

        # Ступень 3: LLM
        if text:
            prompt = (
                "Определи тип и номер нормативного документа из этого текста. "
                "Верни только короткое название на английском, например 'fz_39' или 'ukazanie_5969'.\n\n"
                f"Текст:\n{text[:2000]}"
            )
            try:
                print(f"\033[95m[LLM]\033[0m Запрос к LLM для определения имени ({os.path.basename(filepath)})...")
                response = await acompletion(
                    model=self.llm_model,
                    messages=[{"role": "user", "content": prompt}],
                    vertex_project=self.vertex_project,
                    vertex_location=self.vertex_location,
                    reasoning_effort=self.reasoning_effort
                )
                llm_name = response.choices[0].message.content.strip()
                return self._sanitize_filename(llm_name, extension)
            except Exception as e:
                print(f"\033[93m[Предупреждение]\033[0m LLM fallback failed: {e}")

        # Fallback (не удалось определить)
        print(f"\033[93m[Fallback]\033[0m Не удалось определить имя, используем MD5 файла.")
        md5 = self._calculate_md5(filepath)
        return self._sanitize_filename(f"unknown_{md5[:8]}", extension)

    async def process_file(self, landing_filename: str, rss_title: str = None) -> bool:
        if not os.path.exists(landing_filename):
            print(f"\033[91m[Ошибка]\033[0m Файл {landing_filename} не найден.")
            return False
            
        md5_hash = self._calculate_md5(landing_filename)
        tracker_data = self._load_tracker()
        
        print(f"\033[96m[MD5]\033[0m Файл: {os.path.basename(landing_filename)} | Хеш: {md5_hash}")

        # Глухой фильтр
        if md5_hash in tracker_data.values():
            print(f"\033[93m[Skip]\033[0m Дубликат обнаружен по хешу. Удаляем {landing_filename}")
            os.remove(landing_filename)
            return False

        system_name = await self._determine_system_name(landing_filename, rss_title)
        
        # Слияние
        if system_name in tracker_data:
            old_hash = tracker_data[system_name]
            if old_hash != md5_hash:
                print(f"\033[94m[Update]\033[0m Обнаружена новая редакция для {system_name}.")
            else:
                pass
        else:
            print(f"\033[92m[New]\033[0m Обнаружен новый документ: {system_name}")

        target_path = os.path.join(self.storage_dir, system_name)
        
        print(f"\033[92m[Move]\033[0m Перемещение в хранилище: {target_path}")
        # shutil.move(src, dst) корректно работает даже если dst уже существует
        shutil.move(landing_filename, target_path)
        
        tracker_data[system_name] = md5_hash
        self._save_tracker(tracker_data)
        
        return True
