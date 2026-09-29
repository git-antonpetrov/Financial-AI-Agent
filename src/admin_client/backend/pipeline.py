import os
import hashlib
import requests
import re
from typing import Callable, Any
from core.utils.console_logger import log_info, log_error, log_warning
from core.utils.pdf_converter import convert_to_pdf
from core.utils.content_ai import ContentCaptureRecognizer
class DocumentPipeline:
    """
    Класс, отвечающий за полный жизненный цикл обработки документов перед отправкой на сервер.
    Пайплайн разбит на логические шаги (этапы). 
    """

    def __init__(self, server_url: str, admin_token: str):
        """
        Инициализация пайплайна.
        
        Args:
            server_url (str): Базовый URL сервера (например, https://api.my-vds.com)
            admin_token (str): JWT токен администратора для авторизации запросов
        """
        self.server_url = server_url.rstrip('/')
        self.headers = {"Authorization": f"Bearer {admin_token}"}
        
        # Функция обратного вызова для отправки прогресса во фронтенд (React)
        # Если она не задана, по умолчанию ничего не делает.
        # Сигнатура: callback(filename, status_enum, display_message)
        self.on_progress_update: Callable[[str, str, str], None] = lambda filename, status, message: None
        
        # Ленивая инициализация клиента Content AI
        self._recognizer = None

    def set_progress_callback(self, callback: Callable[[str, str, str], None]):
        """
        Устанавливает коллбэк для общения с интерфейсом.
        
        Args:
            callback: Функция, принимающая (имя_файла, статус, текстовое_сообщение).
                      Примеры статусов: 'processing', 'completed', 'error', 'skipped'
        """
        self.on_progress_update = callback

    def process_file(self, file_path: str, agent_name: str) -> dict[str, Any]:
        """
        Главный метод обработки одного файла. Выполняет все шаги по порядку.
        
        Args:
            file_path (str): Локальный путь к файлу на компьютере администратора
            agent_name (str): Имя RAG-агента (например, 'digital')
            
        Returns:
            dict: Словарь с итоговым результатом обработки
        """
        filename = os.path.basename(file_path)
        log_info("Pipeline", f"Начало обработки файла: {filename}")
        
        # Отправляем в React красивое уведомление о старте
        self.on_progress_update(filename, "processing", "Шаг 1: Вычисление MD5 и проверка дубликатов...")

        try:
            # ==========================================
            # ШАГ 1: ВЫЧИСЛЕНИЕ ХЭША И ПРОВЕРКА НА ДУБЛЬ
            # ==========================================
            file_hash = self._calculate_md5(file_path)
            
            is_duplicate = self._check_duplicate_on_server(filename, file_hash, agent_name)
            if is_duplicate:
                msg = "Пропущен: Точная копия файла уже загружена (дубликат)"
                log_warning("Pipeline", f"Файл {filename} отброшен: {msg}")
                
                # Сигнализируем React'у, что файл нужно покрасить в серый/желтый цвет
                self.on_progress_update(filename, "skipped", msg)
                return {"status": "skipped", "reason": "md5_duplicate"}

            # ==========================================
            # ШАГ 2: КОНВЕРТАЦИЯ В PDF (ПРИ НЕОБХОДИМОСТИ)
            # ==========================================
            working_file_path = file_path
            _, ext = os.path.splitext(file_path)
            if ext.lower() not in ['.pdf']:
                log_info("Pipeline", f"Конвертация {filename} в PDF...")
                self.on_progress_update(filename, "processing", f"Шаг 2: Конвертация {ext} в PDF...")
                # Кладем сконвертированный PDF во временную папку (рядом с оригиналом)
                temp_dir = os.path.join(os.path.dirname(file_path), "temp_converted")
                working_file_path = convert_to_pdf(file_path, temp_dir)
                filename_to_recognize = os.path.basename(working_file_path)
            else:
                filename_to_recognize = filename

            # ==========================================
            # ШАГ 3: РАСПОЗНАВАНИЕ ТЕКСТА (CONTENT AI)
            # ==========================================
            self.on_progress_update(filename, "processing", "Шаг 3: Распознавание текста (Content AI)...")
            log_info("Pipeline", "Отправка документа в Content AI...")
            if not self._recognizer:
                self._recognizer = ContentCaptureRecognizer()
                
            ocr_results = self._recognizer.recognize(working_file_path)
            
            if filename_to_recognize not in ocr_results:
                raise Exception("Content AI не вернул результаты для файла")
                
            parsed_dict = ocr_results[filename_to_recognize]["parsed_dict"]
            recognized_text = self._extract_text_from_parsed_dict(parsed_dict)
            
            if not recognized_text.strip():
                raise Exception("Из документа не удалось извлечь ни одного слова (текст пуст). Возможно файл битый.")

            # ==========================================
            # ШАГ 4: АНАЛИЗ НАЗВАНИЯ И ТИПА ДОКУМЕНТА (GEMINI)
            # ==========================================
            self.on_progress_update(filename, "processing", "Шаг 4: Анализ названия и типа документа (Gemini)...")
            log_info("Pipeline", "Отправка текста на LLM-анализ...")
            
            # Берем первые ~15000 символов, чтобы не превысить лимит токенов (хватит для понимания сути документа)
            analyze_url = f"{self.server_url}/api/llm/analyze"
            analyze_resp = requests.post(
                analyze_url,
                headers=self.headers,
                json={"text": recognized_text[:15000]},
                timeout=(5, 120)
            )
            try:
                analyze_resp.raise_for_status()
            except requests.exceptions.HTTPError as e:
                error_detail = analyze_resp.text
                raise Exception(f"HTTP Error {analyze_resp.status_code}: {error_detail}") from e
                
            analyze_data = analyze_resp.json()
            
            system_name = analyze_data.get("system_name")
            short_name = analyze_data.get("short_name")
            
            if not system_name or not short_name:
                raise Exception(f"LLM вернул некорректный ответ: {analyze_data}")
                
            log_info("Pipeline", f"LLM успешно определил имена: system_name='{system_name}', short_name='{short_name}'")

            # ==========================================
            # ШАГ 5: ПРОВЕРКА ВЕРСИИ ПО ДАТЕ
            # ==========================================
            self.on_progress_update(filename, "processing", "Шаг 5: Проверка актуальности версии...")
            log_info("Pipeline", f"Проверка версии для {short_name}...")
            
            date_url = f"{self.server_url}/api/documents/check/date"
            date_payload = {
                "system_name": system_name,
                "short_name": short_name,
                "file_hash": file_hash,
                "filename": filename,
                "agent_name": agent_name
            }
            
            date_resp = requests.post(date_url, headers=self.headers, json=date_payload, timeout=(5, 120))
            date_resp.raise_for_status()
            
            if date_resp.json().get("status") == "old_version":
                msg = "Пропущен: У нас уже загружена более новая версия этого документа"
                log_warning("Pipeline", f"Файл {filename} отброшен: {msg}")
                self.on_progress_update(filename, "skipped", msg)
                return {"status": "skipped", "reason": "old_version"}
                
            log_info("Pipeline", "Проверка актуальности пройдена (документ новый или свежий)")

            # ==========================================
            # ШАГ 6: ПОИСК ОТМЕНЕННЫХ ДОКУМЕНТОВ
            # ==========================================
            self.on_progress_update(filename, "processing", "Шаг 6: Поиск отмененных актов (Gemini)...")
            log_info("Pipeline", "Поиск абзацев об отмене...")
            
            repealed_paragraphs = self._extract_repeal_paragraphs(recognized_text)
            repealed_short_names = []
            
            if repealed_paragraphs:
                log_info("Pipeline", f"Найдено подозрительных абзацев: {len(repealed_paragraphs)}. Отправляем в LLM...")
                repeal_url = f"{self.server_url}/api/llm/find_repealed"
                repeal_resp = requests.post(
                    repeal_url,
                    headers=self.headers,
                    json={"snippets": repealed_paragraphs},
                    timeout=(5, 120)
                )
                
                if repeal_resp.status_code == 200:
                    repealed_short_names = repeal_resp.json().get("short_names", [])
                    if repealed_short_names:
                        log_info("Pipeline", f"Найдено отмененных актов: {repealed_short_names}")
                else:
                    log_warning("Pipeline", f"Ошибка сервера при поиске отмененных актов: {repeal_resp.text}")

            # ==========================================
            # ШАГ 7: СБОРКА MARKDOWN И ОТПРАВКА НА СЕРВЕР
            # ==========================================
            self.on_progress_update(filename, "processing", "Шаг 7: Подготовка и загрузка на сервер (Векторизация)...")
            log_info("Pipeline", "Формирование Markdown и отправка...")
            
            # Формируем главный файл (upsert)
            markdown_content = f'---\nshort_name: "{short_name}"\nsystem_name: "{system_name}"\n---\n\n{recognized_text}'
            
            upsert_url = f"{self.server_url}/api/upload/{agent_name}/upsert"
            upsert_data = {
                "file_hash": file_hash,
                "filename": filename,
                "system_name": system_name,
                "short_name": short_name
            }
            upsert_files = {
                "file": (f"{system_name}.md", markdown_content.encode('utf-8'), "text/markdown")
            }
            
            log_info("Pipeline", "Вызов ручки /upsert...")
            upsert_resp = requests.post(upsert_url, headers=self.headers, data=upsert_data, files=upsert_files, timeout=(5, 120))
            upsert_resp.raise_for_status()

            # ==========================================
            # ШАГ 8: ОТПРАВКА СПИСКА УСТАРЕВШИХ АКТОВ
            # ==========================================
            if repealed_short_names:
                self.on_progress_update(filename, "processing", "Шаг 8: Отправка списка устаревших актов на удаление...")
                delete_content = "\n".join(repealed_short_names)
                
                delete_url = f"{self.server_url}/api/upload/{agent_name}/delete"
                delete_data = {
                    "file_hash": file_hash,
                    "filename": f"delete_{filename}",
                    "system_name": system_name,
                    "short_name": short_name
                }
                delete_files = {
                    "file": (f"delete_{system_name}.md", delete_content.encode('utf-8'), "text/markdown")
                }
                
                log_info("Pipeline", "Вызов ручки /delete...")
                delete_resp = requests.post(delete_url, headers=self.headers, data=delete_data, files=delete_files, timeout=(5, 120))
                delete_resp.raise_for_status()

            # Успешное завершение всего пайплайна для этого файла!
            success_msg = "Успешно! Документ отправлен в очередь на обработку."
            log_info("Pipeline", success_msg)
            self.on_progress_update(filename, "completed", success_msg)
            
            return {
                "status": "success", 
                "file_hash": file_hash, 
                "system_name": system_name, 
                "short_name": short_name,
                "repealed_docs": repealed_short_names,
                "text_length": len(recognized_text)
            }

        except requests.exceptions.RequestException as e:
            # Отдельно ловим сетевые ошибки (если VDS упал)
            error_msg = f"Ошибка сети при связи с сервером: {str(e)}"
            log_error("Pipeline", f"Сбой в файле {filename} -> {error_msg}")
            self.on_progress_update(filename, "error", error_msg)
            return {"status": "error", "message": error_msg}
            
        except Exception as e:
            error_msg = f"Критическая ошибка: {str(e)}"
            log_error("Pipeline", f"Сбой в файле {filename} -> {error_msg}")
            self.on_progress_update(filename, "error", error_msg)
            return {"status": "error", "message": str(e)}

        finally:
            # Очистка временных файлов PDF
            if 'working_file_path' in locals() and working_file_path != file_path and os.path.exists(working_file_path):
                try:
                    os.remove(working_file_path)
                except Exception as ex:
                    log_warning("Pipeline", f"Не удалось удалить временный файл {working_file_path}: {ex}")

    def _calculate_md5(self, file_path: str) -> str:
        """
        Вспомогательный метод для вычисления MD5 хэша физического файла.
        Читает файл чанками (кусками), чтобы не забивать оперативную память 
        при загрузке гигантских PDF-сканов на гигабайт.
        """
        hasher = hashlib.md5()
        with open(file_path, "rb") as f:
            # Читаем блоками по 4 Мегабайта
            for chunk in iter(lambda: f.read(4096 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def _check_duplicate_on_server(self, filename: str, file_hash: str, agent_name: str) -> bool:
        """
        Делает POST запрос на наш FastAPI сервер для проверки существования документа по MD5.
        
        Returns:
            bool: True если дубликат существует, False если файл новый.
            
        Raises:
            requests.exceptions.RequestException: Если сервер вернул ошибку 500 или недоступен.
        """
        url = f"{self.server_url}/api/documents/check/md5"
        payload = {
            "file_hash": file_hash,
            "filename": filename,
            "agent_name": agent_name
        }
        
        # Отправляем JSON и заголовки с токеном
        response = requests.post(url, headers=self.headers, json=payload, timeout=(5, 120))
        
        # Если статус не 200 (например, 401 Unauthorized или 500 Internal Server Error) — выбрасываем исключение
        response.raise_for_status() 
        
        data = response.json()
        if data.get("status") == "duplicate":
            return True
            
        return False

    def _extract_text_from_parsed_dict(self, parsed_data: Any) -> str:
        """
        Рекурсивно обходит разобранный словарь от Content AI и склеивает все строковые значения.
        Это универсальный метод, который вытянет текст, независимо от того, 
        какая структура полей настроена в проекте FlexiCapture.
        """
        texts = []
        if parsed_data is None:
            return ""
        if isinstance(parsed_data, dict):
            for key, value in parsed_data.items():
                texts.append(self._extract_text_from_parsed_dict(value))
        elif isinstance(parsed_data, list):
            for item in parsed_data:
                texts.append(self._extract_text_from_parsed_dict(item))
        elif isinstance(parsed_data, str):
            texts.append(parsed_data.strip())
        
        # Отфильтруем пустые строки и склеим через пробел/перенос строки
        return "\n".join(filter(bool, texts))

    def _extract_repeal_paragraphs(self, text: str) -> list[str]:
        """
        Ищет абзацы в тексте, где упоминаются слова об отмене других документов.
        Использует регулярное выражение для поиска ключевых слов.
        """
        paragraphs = text.split('\n')
        candidates = []
        for p in paragraphs:
            # Ищем юридические формулировки отмены или признания недействительным
            pattern = r'признать?\s+утративш\w*\s+силу|утрач\w+\s+силу|отменяет(?:ся)?|считать\s+не\s+действующ'
            if re.search(pattern, p, re.IGNORECASE):
                cleaned_p = p.strip()
                if cleaned_p:
                    candidates.append(cleaned_p)
        return candidates
