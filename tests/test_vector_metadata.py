import os
import sys
import yaml

# Mock dependencies before importing vector_worker
from unittest.mock import MagicMock
for mod in ["redis", "minio", "chromadb", "chromadb.config", "litellm", "langchain_text_splitters", "psycopg2", "psycopg2.pool"]:
    sys.modules[mod] = MagicMock()

os.environ["POSTGRES_PASSWORD"] = "test"

sys.path.insert(0, os.path.abspath("."))
sys.path.insert(0, os.path.abspath("src/admin_server"))
sys.path.insert(0, os.path.abspath("src/admin_server/vectors"))

from src.admin_server.vectors.vector_worker import extract_metadata_from_markdown, sanitize_chroma_metadata
import datetime
import json

def test_extract_metadata_basic():
    md = """---
short_name: fz_115
system_name: fz_115_01012024
title: "Закон: О противодействии"
---
# Body content
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
    
    # Ensure all values are strictly str, int, float, or bool
    for k, v in sanitized.items():
        assert isinstance(k, str)
        assert isinstance(v, (str, int, float, bool))
