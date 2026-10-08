import os
import io
import time
import base64
import requests
import xml.etree.ElementTree as ET
import re
from requests.auth import HTTPBasicAuth
try:
    from src.common.logger import log_info, log_error, log_warning
except ImportError:
    from core.utils.console_logger import log_info, log_error, log_warning
from xml.sax.saxutils import escape

def parse_result_xml(xml_str):
    """
    Разбирает XML-результат от Content AI и возвращает его в виде словаря.
    """
    try:
        # Убирает пространства имен для упрощения парсинга
        it = ET.iterparse(io.StringIO(xml_str))
        for _, el in it:
            _, _, el.tag = el.tag.rpartition('}')
        root = it.root
    except Exception:
        return {}
        
    if root is None or root.tag != 'Documents':
        return {}
        
    if not len(root):
        return {}
        
    first_doc = root[0]
    return parse_element(first_doc)

def parse_element(parent):
    """
    Рекурсивно обходит XML элементы и собирает их в словарь.
    """
    if not len(parent):
        return parent.text if parent.text else ""
        
    result = {}
    from collections import defaultdict
    groups = defaultdict(list)
    for child in parent:
        groups[child.tag].append(child)
        
    for tag, elements in groups.items():
        parsed_vals = [parse_element(e) for e in elements]
        if len(parsed_vals) == 1:
            result[tag] = parsed_vals[0]
        else:
            result[tag] = parsed_vals
            
    return result

class ContentCaptureRecognizer:
    """
    Клиент для взаимодействия с сервером Content AI (ABBYY FlexiCapture) через SOAP API.
    Выполняет загрузку, распознавание документов и выгрузку результатов.
    """
    def __init__(self, username: str = "", password: str = "", delete_batch_after=True, api_uri: str = ""):
        self.username = username or os.getenv('CONTENTAI_USERNAME')
        self.password = password or os.getenv('CONTENTAI_PASSWORD')
        if not self.username or not self.password:
            raise ValueError("Не заданы CONTENTAI_USERNAME или CONTENTAI_PASSWORD")
        
        self.api_uri = api_uri or os.getenv('CONTENT_AI_API_URI')
        if not self.api_uri:
            raise ValueError("Не задан CONTENT_AI_API_URI. Укажите его в .env на главном сервере.")
        self.project_name = os.getenv('CONTENT_AI_PROJECT', 'FullText')
        
        self.delete_batch_after = delete_batch_after
        self.max_wait_seconds = int(os.getenv("CONTENTAI_MAX_WAIT_SECONDS", "3600"))
        self.idle_timeout_seconds = int(os.getenv("CONTENTAI_IDLE_TIMEOUT_SECONDS", "600"))
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(self.username, self.password)
        self.headers = {"Content-Type": "text/xml; charset=utf-8"}

    def _call_raw(self, action, body):
        """Отправляет сырой SOAP запрос на сервер."""
        envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
  <s:Header />
  <s:Body>{body}</s:Body>
</s:Envelope>"""
        self.headers["SOAPAction"] = f'"{action}"'
        response = self.session.post(self.api_uri, headers=self.headers, data=envelope.encode("utf-8"))
        if response.status_code != 200:
            log_error("Content AI: SOAP Запрос", f"Вызов {action} завершился с ошибкой {response.status_code}: {response.text}")
            raise Exception(f"SOAP Call {action} failed with status {response.status_code}: {response.text}")
        return response.text
        
    def _call(self, action, body):
        """Отправляет запрос и возвращает распарсенный XML объект."""
        res_text = self._call_raw(action, body)
        return ET.fromstring(res_text)
        
    def recognize(self, file_path: str):
        """
        Запускает процесс распознавания файла на сервере Content AI.
        """
        log_info("Content AI: Подготовка", f"Начало распознавания файла {file_path}")
        if not os.path.exists(file_path):
            log_error("Content AI: Ошибка", f"Файл не найден: {file_path}")
            raise FileNotFoundError(f"Файл не найден: {file_path}")
            
        original_file_name = os.path.basename(file_path)
        xml_file_name = escape(original_file_name)

        # Защита от OOM: ограничение размера файла для SOAP base64 передачи (100 МБ)
        MAX_RECOGNIZE_SIZE = 100 * 1024 * 1024
        file_size = os.path.getsize(file_path)
        if file_size > MAX_RECOGNIZE_SIZE:
            size_mb = file_size // (1024 * 1024)
            error_msg = f"Файл {original_file_name} слишком большой для распознавания ({size_mb} МБ > 100 МБ)"
            log_error("Content AI: Ошибка", error_msg)
            raise ValueError(error_msg)

        with open(file_path, "rb") as f:
            raw_file_bytes = f.read()
            file_bytes = base64.b64encode(raw_file_bytes).decode("ascii")
        del raw_file_bytes
            
        session_id = None
        try:
            log_info("Content AI: Сессия", "Открытие сессии SOAP...")
            res = self._call("#OpenSession", '<OpenSession xmlns="urn:https://www.contentai.ru/ContentCapture"><roleType>1</roleType><stationType>10</stationType></OpenSession>')
            session_node = res.find('.//{urn:https://www.contentai.ru/ContentCapture}sessionId')
            if session_node is None or not session_node.text:
                raise Exception("Content AI не вернул sessionId при вызове OpenSession")
            session_id = session_node.text
            log_info("Content AI: Проекты", "Получение списка проектов...")
            res = self._call("#GetProjects", '<GetProjects xmlns="urn:https://www.contentai.ru/ContentCapture"></GetProjects>')
            project_guid = None
            for proj in res.findall('.//{urn:https://www.contentai.ru/ContentCapture}Project'):
                name = proj.find('{urn:https://www.contentai.ru/ContentCapture}Name').text
                if name == self.project_name:
                    project_guid = proj.find('{urn:https://www.contentai.ru/ContentCapture}Guid').text
                    break
            
            if not project_guid:
                log_error("Content AI: Ошибка", f"Проект '{self.project_name}' не найден на сервере.")
                raise Exception(f"Проект '{self.project_name}' не найден.")
                
            log_info("Content AI: Проект", f"Открытие проекта {self.project_name}...")
            res = self._call("#OpenProject", f'<OpenProject xmlns="urn:https://www.contentai.ru/ContentCapture"><sessionId>{session_id}</sessionId><projectNameOrGuid>{project_guid}</projectNameOrGuid></OpenProject>')
            project_id = res.find('.//{urn:https://www.contentai.ru/ContentCapture}projectId').text
            
            log_info("Content AI: Пакет", "Создание нового пакета документов...")
            batch_xml = f"""<AddNewBatch xmlns="urn:https://www.contentai.ru/ContentCapture">
<sessionId>{session_id}</sessionId>
<projectId>{project_id}</projectId>
<batch>
  <Id>0</Id>
  <Name>API_Batch_{int(time.time())}</Name>
  <ProjectId>{project_id}</ProjectId>
  <BatchTypeId>0</BatchTypeId>
  <Priority>0</Priority>
  <Description></Description>
  <HasAttachments>false</HasAttachments>
  <Properties />
  <CreationDate>0</CreationDate>
  <DocumentsCount>0</DocumentsCount>
  <PagesCount>0</PagesCount>
  <RecognizedSymbolsCount>0</RecognizedSymbolsCount>
  <VerificationSymbolsCount>0</VerificationSymbolsCount>
  <UncertainSymbolsCount>0</UncertainSymbolsCount>
  <AssembledDocumentsCount>0</AssembledDocumentsCount>
  <RecognizedDocumentsCount>0</RecognizedDocumentsCount>
  <VerifiedDocumentsCount>0</VerifiedDocumentsCount>
  <ExportedDocumentsCount>0</ExportedDocumentsCount>
  <StageExternalId>0</StageExternalId>
  <ErrorText></ErrorText>
  <OwnerId>0</OwnerId>
  <CreatorId>0</CreatorId>
  <SLAStartDate>0</SLAStartDate>
  <SLAExpirationDate>0</SLAExpirationDate>
  <ElapsedProcessingSeconds>0</ElapsedProcessingSeconds>
  <HasDocumentResults>false</HasDocumentResults>
</batch>
<ownerId>0</ownerId>
</AddNewBatch>"""
            res = self._call("#AddNewBatch", batch_xml)
            batch_id = res.find('.//{urn:https://www.contentai.ru/ContentCapture}batchId').text
            
            log_info("Content AI: Пакет", f"Открытие пакета {batch_id}...")
            self._call("#OpenBatch", f'<OpenBatch xmlns="urn:https://www.contentai.ru/ContentCapture"><sessionId>{session_id}</sessionId><batchId>{batch_id}</batchId></OpenBatch>')

            log_info("Content AI: Загрузка", f"Отправка файла {original_file_name} на сервер...")
            img_xml = f"""<AddNewImage xmlns="urn:https://www.contentai.ru/ContentCapture">
<sessionId>{session_id}</sessionId>
<batchId>{batch_id}</batchId>
<file>
  <Name>{xml_file_name}</Name>
  <Bytes>{file_bytes}</Bytes>
</file>
</AddNewImage>"""
            self._call("#AddNewImage", img_xml)
            del file_bytes
            del img_xml
            
            log_info("Content AI: Загрузка", f"Закрытие пакета {batch_id} для начала обработки...")
            self._call("#CloseBatch", f'<CloseBatch xmlns="urn:https://www.contentai.ru/ContentCapture"><sessionId>{session_id}</sessionId><batchId>{batch_id}</batchId></CloseBatch>')

            log_info("Content AI: Распознавание", "Запуск обработки пакета на сервере...")
            self._call("#ProcessBatch", f'<ProcessBatch xmlns="urn:https://www.contentai.ru/ContentCapture"><sessionId>{session_id}</sessionId><batchId>{batch_id}</batchId></ProcessBatch>')
            
            log_info("Content AI: Распознавание", "Ожидание завершения обработки...")
            max_wait = self.max_wait_seconds
            idle_timeout = self.idle_timeout_seconds
            elapsed = 0
            idle_seconds = 0
            last_percent = -1
            while elapsed < max_wait:
                res = self._call("#GetBatchPercentCompleted", f'<GetBatchPercentCompleted xmlns="urn:https://www.contentai.ru/ContentCapture"><batchId>{batch_id}</batchId></GetBatchPercentCompleted>')
                result_node = res.find('.//{urn:https://www.contentai.ru/ContentCapture}result')
                if result_node is None or result_node.text is None:
                    raise Exception("Content AI вернул некорректный ответ при проверке прогресса пакета")
                
                percent = int(result_node.text)
                if percent != last_percent:
                    log_info("Content AI: Статус", f"Прогресс распознавания: {percent}% (прошло {elapsed}с)")
                    last_percent = percent
                    idle_seconds = 0
                else:
                    idle_seconds += 2

                if percent == 100:
                    break
                if percent < 0:
                    raise RuntimeError(f"Content AI сообщил о сбое обработки пакета (статус: {percent})")

                if idle_seconds >= idle_timeout:
                    raise TimeoutError(f"Content AI завис: прогресс не меняется ({percent}%) более {idle_timeout} секунд")
                
                # Периодически проверяем наличие ошибок в пакете
                if elapsed > 0 and elapsed % 10 == 0:
                    try:
                        batch_res = self._call("#GetBatch", f'<GetBatch xmlns="urn:https://www.contentai.ru/ContentCapture"><sessionId>{session_id}</sessionId><batchId>{batch_id}</batchId></GetBatch>')
                        err_node = batch_res.find('.//{urn:https://www.contentai.ru/ContentCapture}ErrorText')
                        if err_node is not None and err_node.text and err_node.text.strip():
                            raise RuntimeError(f"Content AI ошибка пакета: {err_node.text.strip()}")
                    except Exception as err:
                        if "Content AI ошибка пакета" in str(err):
                            raise err

                time.sleep(2)
                elapsed += 2
            else:
                raise TimeoutError(f"Content AI не завершил обработку за {max_wait} секунд")
                
            log_info("Content AI: Результат", "Получение распознанных документов...")
            docs_xml_raw = self._call_raw("#GetDocuments", f'<GetDocuments xmlns="urn:https://www.contentai.ru/ContentCapture"><sessionId>{session_id}</sessionId><batchId>{batch_id}</batchId></GetDocuments>')
            docs_xml = ET.fromstring(docs_xml_raw)
            documents = docs_xml.findall('.//{urn:https://www.contentai.ru/ContentCapture}Document')
            
            results = {}
            for doc in documents:
                doc_id = doc.find('{urn:https://www.contentai.ru/ContentCapture}Id').text
                log_info("Content AI: Результат", f"Скачивание XML с результатом для документа {doc_id}...")
                
                try:
                    res_xml_raw = self._call_raw("#LoadDocumentResult", f'<LoadDocumentResult xmlns="urn:https://www.contentai.ru/ContentCapture"><sessionId>{session_id}</sessionId><batchId>{batch_id}</batchId><documentId>{doc_id}</documentId><fileName>Result.xml</fileName></LoadDocumentResult>')
                    res_xml = ET.fromstring(res_xml_raw)
                    xml_node = res_xml.find('.//{urn:https://www.contentai.ru/ContentCapture}XmlResult')
                    
                    doc_content = None
                    if xml_node is not None and xml_node.text:
                        doc_content = base64.b64decode(xml_node.text).decode('utf-8', errors='ignore')
                    else:
                        # Парсинг base64 через regex для обхода проблем с XML namespaces
                        bytes_match = re.search(r'<Bytes>(.*?)</Bytes>', res_xml_raw, re.DOTALL)
                        if bytes_match and bytes_match.group(1):
                            xml_bytes = base64.b64decode(bytes_match.group(1))
                            doc_content = xml_bytes.decode('utf-8', errors='replace')
                        else:
                            # Парсинг через ElementTree как запасной вариант
                            bytes_node = res_xml.find('.//{urn:https://www.contentai.ru/ContentCapture}Bytes')
                            if bytes_node is not None and bytes_node.text:
                                xml_bytes = base64.b64decode(bytes_node.text)
                                doc_content = xml_bytes.decode('utf-8', errors='replace')
                    
                    if doc_content:
                        results[original_file_name] = {
                            "parsed_dict": parse_result_xml(doc_content),
                            "raw_xml": doc_content
                        }
                except Exception as e:
                    log_error("Content AI: Результат", f"Ошибка при загрузке документа {doc_id}: {e}")
                
            if self.delete_batch_after:
                log_info("Content AI: Очистка", "Удаление пакета с сервера...")
                self._call("#DeleteBatch", f'<DeleteBatch xmlns="urn:https://www.contentai.ru/ContentCapture"><sessionId>{session_id}</sessionId><batchId>{batch_id}</batchId></DeleteBatch>')
            
            return results
                
        finally:
            if session_id:
                try:
                    log_info("Content AI: Сессия", "Закрытие SOAP сессии...")
                    self._call("#CloseSession", f'<CloseSession xmlns="urn:https://www.contentai.ru/ContentCapture"><sessionId>{session_id}</sessionId></CloseSession>')
                except Exception as ex:
                    log_warning("Content AI: Сессия", f"Ошибка при закрытии SOAP сессии: {ex}")
