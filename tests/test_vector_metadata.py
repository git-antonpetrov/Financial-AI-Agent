import os
import sys
from unittest.mock import MagicMock

# Мокирует внешние сервисы только если они отсутствуют в системе
for mod in ["redis", "psycopg2", "psycopg2.pool"]:
    if mod not in sys.modules:
        try:
            __import__(mod)
        except ImportError:
            sys.modules[mod] = MagicMock()

os.environ["POSTGRES_PASSWORD"] = "test"

sys.path.insert(0, os.path.abspath("."))
sys.path.insert(0, os.path.abspath("src/admin_server"))
sys.path.insert(0, os.path.abspath("src/admin_server/vectors"))

from src.admin_server.vectors.vector_worker import extract_metadata_from_markdown, sanitize_chroma_metadata, process_task
import datetime
import json

def test_extract_metadata_basic():
    md = """---
short_name: fz_115
system_name: fz_115_01012024
title: "Закон: О противодействии"
---
# Тело документа
Here is text.
"""
    meta = extract_metadata_from_markdown(md)
    assert meta["short_name"] == "fz_115"
    assert meta["system_name"] == "fz_115_01012024"
    assert meta["title"] == "Закон: О противодействии"

def test_extract_metadata_crlf_and_bom():
    md = "\ufeff---\r\nshort_name: doc_test\r\nnumber: 42\r\n---\r\nBody"
    meta = extract_metadata_from_markdown(md)
    assert meta["short_name"] == "doc_test"
    assert meta["number"] == 42

def test_extract_metadata_malformed():
    md = """---
short_name: [unclosed list
---
Body
"""
    meta = extract_metadata_from_markdown(md)
    assert meta == {}

def test_extract_metadata_no_frontmatter():
    md = "# Just a header\nNo frontmatter here."
    meta = extract_metadata_from_markdown(md)
    assert meta == {}

def test_sanitize_chroma_metadata():
    raw = {
        "short_name": "fz_115",
        "version": 1,
        "score": 0.95,
        "is_active": True,
        "null_val": None,
        "tags": ["law", "finance"],
        "nested": {"sub": "value"},
        "date_field": datetime.date(2024, 1, 1),
        123: "numeric_key"
    }
    sanitized = sanitize_chroma_metadata(raw)

    assert "null_val" not in sanitized
    assert sanitized["short_name"] == "fz_115"
    assert sanitized["version"] == 1
    assert sanitized["score"] == 0.95
    assert sanitized["is_active"] is True
    assert sanitized["tags"] == json.dumps(["law", "finance"], ensure_ascii=False)
    assert sanitized["nested"] == json.dumps({"sub": "value"}, ensure_ascii=False)
    assert sanitized["date_field"] == "2024-01-01"
    assert sanitized["123"] == "numeric_key"

    # Проверяет, что все значения строго скалярных типов: str, int, float или bool
    for k, v in sanitized.items():
        assert isinstance(k, str)
        assert isinstance(v, (str, int, float, bool))

def test_process_task_preserves_minio_file_on_error(monkeypatch):
    """Проверяет сохранение файла в MinIO при возникновении ошибки обработки."""
    from src.admin_server.vectors import vector_worker

    mock_client = MagicMock()
    monkeypatch.setattr(vector_worker, "minio_client", mock_client)
    monkeypatch.setattr(vector_worker, "get_chroma_client", lambda: MagicMock())
    # Имитирует возврат некорректного Markdown без short_name
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"# No frontmatter"
    mock_client.get_object.return_value = mock_resp

    task = {
        "agent": "bank",
        "action": "upsert",
        "file_path": "bank/doc.md",
        "bucket": "docs",
        "file_hash": "hash123"
    }
    process_task(task)

    # Файл не должен удаляться при сбое
    mock_client.remove_object.assert_not_called()

def test_process_task_deletes_minio_file_on_success(monkeypatch):
    """Проверяет удаление файла из MinIO после успешного выполнения задачи."""
    from src.admin_server.vectors import vector_worker

    mock_client = MagicMock()
    monkeypatch.setattr(vector_worker, "minio_client", mock_client)
    monkeypatch.setattr(vector_worker, "get_chroma_client", lambda: MagicMock())
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"doc_to_delete\n"
    mock_client.get_object.return_value = mock_resp

    task = {
        "agent": "bank",
        "action": "delete",
        "file_path": "bank/delete_list.txt",
        "bucket": "docs",
        "file_hash": "hash456"
    }
    process_task(task)

    # Файл должен удаляться только после успешного выполнения
    vector_worker.minio_client.remove_object.assert_called_once_with("docs", "bank/delete_list.txt")
