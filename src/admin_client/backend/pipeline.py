import os
import hashlib
import json
import requests
import re
import yaml
from typing import Callable, Any, Optional
try:
    from src.common.logger import log_info, log_error, log_warning
except ImportError:
    from core.utils.console_logger import log_info, log_error, log_warning

try:
    from src.admin_client.backend.core.utils.pdf_converter import convert_to_pdf
    from src.admin_client.backend.core.utils.content_ai import ContentCaptureRecognizer
except ImportError:
    from core.utils.pdf_converter import convert_to_pdf
    from core.utils.content_ai import ContentCaptureRecognizer

try:
    from src.admin_client.backend.core.crypto import ZeroTrustClientSigner
except ImportError:
    try:
        from core.crypto import ZeroTrustClientSigner
    except ImportError:
        ZeroTrustClientSigner = None

import threading

_ACTIVE_MD5_LOCK = threading.Lock()
_ACTIVE_MD5S = set()

class DocumentPipeline:
    """
    Выполняет полный жизненный цикл обработки документов перед отправкой на сервер.
    Пайплайн разбит на последовательные логические этапы с поддержкой Zero-Trust mTLS и цифровой подписи.
    """

    def __init__(
        self,
        server_url: str,
        admin_token: str,
        contentai_username: str = "",
        contentai_password: str = "",
        contentai_api_uri: str = "",
        ca_cert_path: Optional[str] = None,
        client_cert_path: Optional[str] = None,
        client_key_path: Optional[str] = None,
        signer: Optional[Any] = None,
        use_zero_trust: bool = True
    ):
        """
        Инициализирует экземпляр пайплайна с параметрами подключения к серверу, Content AI и mTLS.
        
        Args:
            server_url (str): Базовый URL сервера (например, https://api.my-vds.com)
            admin_token (str): JWT токен администратора для авторизации запросов
            ca_cert_path (Optional[str]): Путь к доверенному Root CA сертификату (ca.crt)
            client_cert_path (Optional[str]): Путь к клиентскому mTLS сертификату (admin_client.crt)
            client_key_path (Optional[str]): Путь к приватному ключу клиента (admin_client.key)
            signer (Optional[ZeroTrustClientSigner]): Экземпляр клиента цифровой подписи
            use_zero_trust (bool): Использовать защищенный эндпоинт /api/v1/rag/documents с цифровой подписью
        """
        self.server_url = server_url.rstrip('/')
        self.headers = {"Authorization": f"Bearer {admin_token}"}
        self.contentai_username = contentai_username
        self.contentai_password = contentai_password
        self.contentai_api_uri = contentai_api_uri
        self.use_zero_trust = use_zero_trust

        # Zero-Trust mTLS & PKI конфигурация
        self.ca_cert_path = ca_cert_path or os.getenv("CA_CERT_PATH")
        if not self.ca_cert_path or not os.path.exists(self.ca_cert_path):
            for default_ca in ["./certs/ca.crt", "/certs/ca.crt", "certs/ca.crt"]:
                if os.path.exists(default_ca):
                    self.ca_cert_path = default_ca
                    break

        self.client_cert_path = client_cert_path or os.getenv("ADMIN_CLIENT_CERT_PATH")
        if not self.client_cert_path or not os.path.exists(self.client_cert_path):
            for default_cert in ["./certs/admin_client.crt", "/certs/admin_client.crt", "certs/admin_client.crt"]:
                if os.path.exists(default_cert):
                    self.client_cert_path = default_cert
                    break

        self.client_key_path = client_key_path or os.getenv("ADMIN_CLIENT_KEY_PATH")
        if not self.client_key_path or not os.path.exists(self.client_key_path):
            for default_key in ["./certs/admin_client.key", "/certs/admin_client.key", "certs/admin_client.key"]:
                if os.path.exists(default_key):
                    self.client_key_path = default_key
                    break

        # Настройка защищенной сессии requests с mTLS
        self.session = requests.Session()
        if self.ca_cert_path and os.path.exists(self.ca_cert_path):
            self.session.verify = self.ca_cert_path
        if (
            self.client_cert_path
            and self.client_key_path
            and os.path.exists(self.client_cert_path)
            and os.path.exists(self.client_key_path)
        ):
            self.session.cert = (self.client_cert_path, self.client_key_path)

        # Инициализация ZeroTrustClientSigner
        if signer is not None:
            self.signer = signer
        elif ZeroTrustClientSigner is not None:
            try:
                self.signer = ZeroTrustClientSigner(
                    cert_path=self.client_cert_path,
                    key_path=self.client_key_path,
                    ca_cert_path=self.ca_cert_path
                )
            except Exception as e:
                log_warning("Pipeline", f"Не удалось инициализировать ZeroTrustClientSigner: {e}")
                self.signer = None
        else:
            self.signer = None
        
        # Функция обратного вызова для отправки прогресса во фронтенд (React)
        # Если она не задана, по умолчанию ничего не делает.
        # Сигнатура: callback(filename, status_enum, display_message)
        self.on_progress_update: Callable[[str, str, str], None] = lambda filename, status, message: None
        
        # Ленивая инициализация клиента Content AI
        self._recognizer = None

    def set_progress_callback(self, callback: Callable[[str, str, str], None]):
        """
        Устанавливает функцию обратного вызова для передачи статуса обработки в пользовательский интерфейс.
        
        Args:
            callback: Функция, принимающая (имя_файла, статус, текстовое_сообщение).
                      Примеры статусов: 'processing', 'completed', 'error', 'skipped'
        """
        self.on_progress_update = callback

    def process_file(self, file_path: str, agent_name: str) -> dict[str, Any]:
        """
        Обрабатывает отдельный документ, последовательно выполняя все шаги пайплайна.
        
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

        is_hash_locked = False
        try:
            # ==========================================
            # ШАГ 1: ВЫЧИСЛЕНИЕ ХЭША И ПРОВЕРКА НА ДУБЛЬ
            # ==========================================
            file_hash = self._calculate_md5(file_path)
            
            with _ACTIVE_MD5_LOCK:
                if file_hash in _ACTIVE_MD5S:
                    msg = "Пропущен: Точная копия файла уже находится в процессе обработки локально"
                    log_warning("Pipeline", f"Файл {filename} отброшен (локальный дубликат): {msg}")
                    self.on_progress_update(filename, "skipped", msg)
                    return {"status": "skipped", "reason": "md5_duplicate_local"}
                _ACTIVE_MD5S.add(file_hash)
                is_hash_locked = True
                
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
                self._recognizer = ContentCaptureRecognizer(username=self.contentai_username, password=self.contentai_password, api_uri=self.contentai_api_uri)
                
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
            analyze_resp = self.session.post(
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
            
            date_resp = self.session.post(date_url, headers=self.headers, json=date_payload, timeout=(5, 120))
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
                repeal_resp = self.session.post(
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
            
            # Формируем главный файл (upsert) с безопасной сериализацией YAML frontmatter
            frontmatter_data = {
                "short_name": str(short_name),
                "system_name": str(system_name)
            }
            frontmatter_yaml = yaml.safe_dump(frontmatter_data, allow_unicode=True, default_flow_style=False).strip()
            markdown_content = f'---\n{frontmatter_yaml}\n---\n\n{recognized_text}'
            
            # Санитизация system_name для безопасного имени файла в multipart-запросе
            safe_system_name = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', str(system_name)).strip('._')
            if not safe_system_name:
                safe_system_name = f"doc_{file_hash[:8]}"

            # Приоритетный путь Zero-Trust с цифровой подписью документа
            zero_trust_used = False
            ack_receipt = None
            if self.use_zero_trust and self.signer:
                try:
                    log_info("Pipeline", "Отправка документа через Zero-Trust RAG эндпоинт /api/v1/rag/documents...")
                    ack_receipt = self.submit_zero_trust_document(
                        document_title=str(system_name),
                        content=markdown_content,
                        collection_name=f"knowledge-{agent_name}",
                        metadata={
                            "source": "admin_workstation",
                            "agent_name": agent_name,
                            "short_name": str(short_name),
                            "file_hash": file_hash,
                            "filename": filename
                        }
                    )
                    zero_trust_used = True
                    log_info("Pipeline", f"Документ успешно передан в RAG через Zero-Trust (receipt_id={ack_receipt.get('receipt_id')})")
                except requests.exceptions.HTTPError as he:
                    if he.response is not None and he.response.status_code == 404:
                        log_warning("Pipeline", "Эндпоинт /api/v1/rag/documents недоступен (404), используем fallback /upsert")
                    else:
                        raise he
                except Exception as zte:
                    log_warning("Pipeline", f"Предупреждение Zero-Trust отправки: {zte}, откат к /upsert...")

            if not zero_trust_used:
                upsert_url = f"{self.server_url}/api/upload/{agent_name}/upsert"
                upsert_data = {
                    "file_hash": file_hash,
                    "system_name": system_name,
                    "short_name": short_name
                }
                upsert_files = {
                    "file": (f"{safe_system_name}.md", markdown_content.encode('utf-8'), "text/markdown")
                }
                
                log_info("Pipeline", "Вызов ручки /upsert...")
                upsert_resp = self.session.post(upsert_url, headers=self.headers, data=upsert_data, files=upsert_files, timeout=(5, 120))
                upsert_resp.raise_for_status()

            # ==========================================
            # ШАГ 8: ОТПРАВКА СПИСКА УСТАРЕВШИХ АКТОВ
            # ==========================================
            if repealed_short_names:
                self.on_progress_update(filename, "processing", "Шаг 8: Отправка списка устаревших актов на удаление...")
                delete_content = "\n".join(repealed_short_names)
                delete_hash = hashlib.md5(f"del_{file_hash}_{delete_content}".encode('utf-8')).hexdigest()
                
                delete_url = f"{self.server_url}/api/upload/{agent_name}/delete"
                delete_data = {
                    "file_hash": delete_hash,
                    "system_name": f"delete_{system_name}",
                    "short_name": short_name
                }
                delete_files = {
                    "file": (f"delete_{safe_system_name}.md", delete_content.encode('utf-8'), "text/markdown")
                }
                
                log_info("Pipeline", "Вызов ручки /delete...")
                delete_resp = self.session.post(delete_url, headers=self.headers, data=delete_data, files=delete_files, timeout=(5, 120))
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
                "text_length": len(recognized_text),
                "zero_trust": zero_trust_used,
                "receipt": ack_receipt
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
            # Освобождаем локальную блокировку MD5 только если этот поток её установил
            if is_hash_locked and 'file_hash' in locals():
                with _ACTIVE_MD5_LOCK:
                    _ACTIVE_MD5S.discard(file_hash)
                    
            # Очистка временных файлов PDF
            if 'working_file_path' in locals() and working_file_path != file_path and os.path.exists(working_file_path):
                try:
                    os.remove(working_file_path)
                except Exception as ex:
                    log_warning("Pipeline", f"Не удалось удалить временный файл {working_file_path}: {ex}")

    def submit_zero_trust_document(
        self,
        document_title: str,
        content: str,
        collection_name: str = "financial_kb",
        metadata: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        """
        Отправляет документ в защищенный эндпоинт RAG (POST /api/v1/rag/documents)
        с цифровой подписью X-Signature, Nonce, Timestamp и валидацией серверной квитанции AckReceipt.
        """
        if not self.signer:
            raise RuntimeError("ZeroTrustClientSigner не инициализирован")

        payload = {
            "document_title": document_title,
            "collection_name": collection_name,
            "content": content,
            "metadata": metadata or {}
        }
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        # Получаем защитные заголовки Zero-Trust
        zt_headers = self.signer.create_rag_request_headers(body_bytes)
        combined_headers = {
            **self.headers,
            **zt_headers,
            "Content-Type": "application/json"
        }

        url = f"{self.server_url}/api/v1/rag/documents"
        resp = self.session.post(url, data=body_bytes, headers=combined_headers, timeout=(5, 120))
        resp.raise_for_status()

        resp_data = resp.json()

        # Валидация подписи сервера в квитанции
        server_sig = resp_data.get("signature") or resp.headers.get("X-Admin-Signature", "")
        receipt_verified = False
        if server_sig:
            receipt_verified = self.signer.verify_server_receipt(resp_data, server_sig)
            if not receipt_verified:
                log_warning("Pipeline", f"Предупреждение: подпись квитанции сервера {resp_data.get('receipt_id')} не подтверждена")
            else:
                log_info("Pipeline", f"Квитанция сервера AckReceipt подтверждена криптографически: receipt_id={resp_data.get('receipt_id')}")

        resp_data["receipt_verified"] = receipt_verified
        return resp_data

    def _calculate_md5(self, file_path: str) -> str:
        """
        Вычисляет хэш-сумму MD5 для локального файла блочным чтением для экономии оперативной памяти.
        """
        hasher = hashlib.md5()
        with open(file_path, "rb") as f:
            # Читаем блоками по 4 Мегабайта
            for chunk in iter(lambda: f.read(4096 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def _check_duplicate_on_server(self, filename: str, file_hash: str, agent_name: str) -> bool:
        """
        Отправляет POST-запрос к FastAPI серверу для проверки наличия дубликата документа по MD5.
        
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
        
        # Отправляем JSON и заголовки с токеном через защищенную mTLS-сессию
        response = self.session.post(url, headers=self.headers, json=payload, timeout=(5, 120))
        
        # Если статус не 200 (например, 401 Unauthorized или 500 Internal Server Error) — выбрасываем исключение
        response.raise_for_status() 
        
        data = response.json()
        if data.get("status") == "duplicate":
            return True
            
        return False

    def _extract_text_from_parsed_dict(self, parsed_data: Any) -> str:
        """
        Рекурсивно обходит структуру разобранного словаря Content AI и извлекает все строковые значения.
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
        elif isinstance(parsed_data, (int, float)):
            texts.append(str(parsed_data))
        
        # Отфильтруем пустые строки и склеим через пробел/перенос строки
        return "\n".join(filter(bool, texts))

    def _extract_repeal_paragraphs(self, text: str) -> list[str]:
        """
        Выделяет абзацы текста с юридическими формулировками об отмене или утрате силы нормативных актов.
        """
        paragraphs = text.split('\n')
        candidates = []
        for p in paragraphs:
            # Ищем юридические формулировки отмены или признания недействительным
            pattern = r'(?:призна\w*|считать)\s+(?:утративш\w*|недействительн\w*)\s+силу|утра(?:чива\w*|тил\w*)\s+силу|отмен(?:яет(?:ся)?|ить)\b|считать\s+не\s*действующ\w*'
            if re.search(pattern, p, re.IGNORECASE):
                cleaned_p = p.strip()
                if cleaned_p:
                    candidates.append(cleaned_p)
        return candidates
