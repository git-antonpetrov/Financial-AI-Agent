"""
Тесты конвертного шифрования (Envelope Encryption) для ChromaDB.
Проверяют:
- Шифрование и расшифровку чанков через AES-256-GCM;
- Защиту от подделки (tamper detection) и контроль целостности;
- Обратную совместимость с унаследованными открытыми чанками (legacy plaintext);
- Шифрование чувствительных метаданных с сохранением индексных полей (short_name, system_name);
- Прозрачную расшифровку результатов запросов ChromaDB (collection.query и collection.get);
- Интеграцию с vector_worker (гарантия того, что в ChromaDB никогда не попадает открытый текст);
- Конфигурацию docker-compose.yml и .env.example.
"""

import os
import sys
import yaml
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Предварительный импорт fastapi до любых модификаций sys.path
import fastapi

# Настройка путей для импорта
sys.path.insert(0, os.path.abspath("src/admin_server"))
sys.path.insert(0, os.path.abspath("src/admin_server/fastapi"))
sys.path.insert(0, os.path.abspath("src/admin_server/vectors"))

# Обязательные переменные окружения
os.environ["POSTGRES_PASSWORD"] = "test_postgres_secret"

# Изоляция зависимостей окружения
for mod in ["redis", "litellm", "asyncpg", "psycopg2", "psycopg2.pool", "chromadb", "chromadb.config"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

from vectors.chroma_envelope import (
    is_envelope_encryption_enabled,
    resolve_encryption_key,
    encrypt_chroma_chunk,
    decrypt_chroma_chunk,
    encrypt_chroma_metadata,
    decrypt_chroma_metadata,
    encrypt_batch,
    decrypt_chroma_results,
    MAGIC_ENVELOPE_V1,
)


def test_encrypt_decrypt_chroma_chunk_roundtrip():
    """Проверяет раундтрип шифрования и расшифровки чанков документа."""
    key = b"1" * 32
    chunks = [
        "Статья 7. Обязанности финансовых организаций по идентификации клиентов.",
        "# Внутренний регламент\nПараграф 2.1: Проведение проверок по ПОД/ФТ.",
        "Markdown with tables:\n| Код | Значение |\n| 01 | Успешно |",
        "Специальные символы: !@#$%^&*()_+-=[]{}|;':\",./<>?`~",
        "Emoji и мультиязычность: 🏦 Банк России • Basel III standards",
    ]
    for chunk in chunks:
        encrypted_chunk = encrypt_chroma_chunk(chunk, key=key)
        assert isinstance(encrypted_chunk, str)
        # Зашифрованный чанк всегда начинается с Base64url-префикса "RU5D" (b"ENC1")
        assert encrypted_chunk.startswith("RU5D")
        decrypted_chunk = decrypt_chroma_chunk(encrypted_chunk, key=key)
        assert decrypted_chunk == chunk


def test_chroma_chunk_tamper_detection():
    """Проверяет отклонение поврежденных или модифицированных чанков."""
    key = b"2" * 32
    original = "Конфиденциальный финансовый отчет казначейства"
    encrypted = encrypt_chroma_chunk(original, key=key)

    import base64
    raw = bytearray(base64.urlsafe_b64decode(encrypted.encode("ascii")))

    # 1. Повреждение байта шифротекста
    raw[20] ^= 0x01
    tampered_str = base64.urlsafe_b64encode(raw).decode("ascii")

    with pytest.raises(ValueError, match="Ошибка расшифровки чанка ChromaDB"):
        decrypt_chroma_chunk(tampered_str, key=key)

    # 2. Попытка расшифровать чужим ключом
    wrong_key = b"9" * 32
    with pytest.raises(ValueError, match="Ошибка расшифровки чанка ChromaDB"):
        decrypt_chroma_chunk(encrypted, key=wrong_key)


def test_legacy_plaintext_chunk_compatibility():
    """Проверяет прозрачную работу со старыми незашифрованными чанками."""
    legacy_chunks = [
        "Обычный текстовый чанк без шифрования",
        "# Header 1\nSome text here",
        "Short note",
        "",
        None,
    ]
    for chunk in legacy_chunks:
        # Для строк, не начинающихся с RU5D, функция возвращает исходную строку
        assert decrypt_chroma_chunk(chunk) == chunk  # type: ignore


def test_encrypt_decrypt_chroma_metadata():
    """Проверяет шифрование чувствительных метаданных с сохранением ключевых индексных полей."""
    key = b"3" * 32
    raw_metadata = {
        "short_name": "fz_115",
        "system_name": "fz_115_01012026",
        "chunk_index": 4,
        "title": "Федеральный закон о противодействии легализации",
        "description": "Секретная инструкция для комплаенс-контроля",
        "is_active": True,
        "score": 0.99,
    }

    encrypted_metadata = encrypt_chroma_metadata(raw_metadata, key=key)

    # 1. Индексные поля сохранены в открытом виде для where-фильтров ChromaDB
    assert encrypted_metadata["short_name"] == "fz_115"
    assert encrypted_metadata["system_name"] == "fz_115_01012026"
    assert encrypted_metadata["chunk_index"] == 4
    assert encrypted_metadata["is_active"] is True
    assert encrypted_metadata["score"] == 0.99
    assert encrypted_metadata["is_encrypted"] is True

    # 2. Чувствительные текстовые поля зашифрованы
    assert encrypted_metadata["title"].startswith("RU5D")
    assert encrypted_metadata["description"].startswith("RU5D")
    assert encrypted_metadata["title"] != raw_metadata["title"]

    # 3. Полная расшифровка метаданных
    decrypted_metadata = decrypt_chroma_metadata(encrypted_metadata, key=key)
    assert decrypted_metadata["title"] == raw_metadata["title"]
    assert decrypted_metadata["description"] == raw_metadata["description"]
    assert decrypted_metadata["short_name"] == "fz_115"


def test_encrypt_batch():
    """Проверяет синхронное шифрование батча чанков и списка метаданных."""
    key = b"4" * 32
    chunks = ["Чанк 1: вводная часть", "Чанк 2: основная часть", "Чанк 3: заключение"]
    metas = [
        {"short_name": "doc_1", "title": "Документ 1"},
        {"short_name": "doc_1", "title": "Документ 1"},
        {"short_name": "doc_1", "title": "Документ 1"},
    ]

    enc_chunks, enc_metas = encrypt_batch(chunks, metas, key=key)
    assert len(enc_chunks) == len(chunks)
    assert len(enc_metas) == len(metas)

    for c in enc_chunks:
        assert c.startswith("RU5D")
    for m in enc_metas:
        assert m["short_name"] == "doc_1"
        assert m["title"].startswith("RU5D")
        assert m["is_encrypted"] is True


def test_decrypt_chroma_results_query_nested():
    """Проверяет расшифровку вложенной структуры ответа collection.query()."""
    key = b"5" * 32
    enc_chunk1 = encrypt_chroma_chunk("Секретный фрагмент А", key=key)
    enc_chunk2 = encrypt_chroma_chunk("Секретный фрагмент Б", key=key)
    enc_title = encrypt_chroma_chunk("Заголовок документа", key=key)

    query_results = {
        "ids": [["doc_1_chunk_0", "doc_1_chunk_1"]],
        "documents": [[enc_chunk1, enc_chunk2]],
        "metadatas": [[{"short_name": "doc_1", "title": enc_title}, {"short_name": "doc_1", "title": enc_title}]],
        "distances": [[0.05, 0.12]],
    }

    decrypted = decrypt_chroma_results(query_results, key=key)
    assert decrypted["documents"] == [["Секретный фрагмент А", "Секретный фрагмент Б"]]
    assert decrypted["metadatas"][0][0]["title"] == "Заголовок документа"
    assert decrypted["metadatas"][0][1]["title"] == "Заголовок документа"
    assert decrypted["metadatas"][0][0]["short_name"] == "doc_1"


def test_decrypt_chroma_results_get_flat():
    """Проверяет расшифровку плоской структуры ответа collection.get()."""
    key = b"6" * 32
    enc_chunk = encrypt_chroma_chunk("Текст документа из get()", key=key)
    enc_title = encrypt_chroma_chunk("Название документа", key=key)

    get_results = {
        "ids": ["doc_chunk_1"],
        "documents": [enc_chunk],
        "metadatas": [{"short_name": "doc", "title": enc_title}],
    }

    decrypted = decrypt_chroma_results(get_results, key=key)
    assert decrypted["documents"] == ["Текст документа из get()"]
    assert decrypted["metadatas"][0]["title"] == "Название документа"
    assert decrypted["metadatas"][0]["short_name"] == "doc"


def test_associated_data_aead_binding():
    """Проверяет связывание чанка с идентификатором документа (AAD)."""
    key = b"7" * 32
    text = "Конфиденциальные лимиты межбанковского кредитования"
    aad = "bank_doc_fz_88"

    encrypted = encrypt_chroma_chunk(text, key=key, aad=aad)
    decrypted = decrypt_chroma_chunk(encrypted, key=key, aad=aad)
    assert decrypted == text

    # Несовпадение AAD вызывает отказ расшифровки
    with pytest.raises(ValueError, match="Ошибка расшифровки"):
        decrypt_chroma_chunk(encrypted, key=key, aad="wrong_doc_id")


def test_vector_worker_stores_only_encrypted_chunks():
    """
    Интеграционный тест: гарантирует, что vector_worker при добавлении в ChromaDB
    передает в collection.add строго зашифрованные чанки (Envelope Encryption).
    """
    from vectors import vector_worker

    # Мокируем зависимости
    mock_collection = MagicMock()
    vector_worker.chroma_client.get_or_create_collection.return_value = mock_collection
    vector_worker.get_embeddings_google = MagicMock(return_value=[[0.1] * 768])
    vector_worker.update_document_status = MagicMock()
    if hasattr(vector_worker.RecursiveCharacterTextSplitter, "return_value"):
        vector_worker.RecursiveCharacterTextSplitter.return_value.split_text.return_value = [
            "Запрещается выдача необеспеченных кредитов свыше 100 млн рублей."
        ]

    mock_resp = MagicMock()
    mock_resp.read.return_value = """---
short_name: credit_policy
system_name: credit_policy_01012026
title: "Внутренняя кредитная политика банка"
---
# Раздел 1. Максимальные лимиты риска.
Запрещается выдача необеспеченных кредитов свыше 100 млн рублей.
""".encode("utf-8")
    vector_worker.minio_client = MagicMock()
    vector_worker.minio_client.get_object.return_value = mock_resp

    task = {
        "agent": "bank",
        "action": "upsert",
        "file_path": "upsert/credit_policy.md",
        "bucket": "knowledge-bank",
        "file_hash": "hash_sec_999",
    }

    test_key = "a" * 64
    with patch.dict(os.environ, {"CHROMA_ENVELOPE_ENCRYPTION_ENABLED": "true", "DATA_ENCRYPTION_KEY": test_key}, clear=False):
        vector_worker.process_task(task)

    # Проверяем вызов collection.add
    mock_collection.add.assert_called_once()
    _, add_kwargs = mock_collection.add.call_args

    stored_docs = add_kwargs["documents"]
    stored_metas = add_kwargs["metadatas"]

    # 1. Ни один открытый чанк не попал в ChromaDB! Все чанки начинаются с префикса ENC1
    assert len(stored_docs) > 0
    for doc in stored_docs:
        assert isinstance(doc, str)
        assert doc.startswith("RU5D"), "Чанк в ChromaDB должен быть зашифрован (RU5D/ENC1)!"
        assert "необеспеченных кредитов" not in doc, "В ChromaDB обнаружен открытый текст!"

    # 2. Чувствительные метаданные (title) зашифрованы
    for meta in stored_metas:
        assert meta["short_name"] == "credit_policy"
        assert meta["is_encrypted"] is True
        assert meta["title"].startswith("RU5D")
        assert "Внутренняя кредитная политика" not in meta["title"]

    # 3. Проверяем, что результаты расшифровываются обратно
    decrypted_docs = [decrypt_chroma_chunk(d, key=test_key) for d in stored_docs]
    assert any("необеспеченных кредитов" in d for d in decrypted_docs)


def test_envelope_encryption_disabled_toggle():
    """Проверяет возможность отключения конвертного шифрования через флаг окружения."""
    with patch.dict(os.environ, {"CHROMA_ENVELOPE_ENCRYPTION_ENABLED": "false"}):
        assert is_envelope_encryption_enabled() is False

    with patch.dict(os.environ, {"CHROMA_ENVELOPE_ENCRYPTION_ENABLED": "true"}):
        assert is_envelope_encryption_enabled() is True


def test_docker_compose_and_env_config():
    """Проверяет наличие флага CHROMA_ENVELOPE_ENCRYPTION_ENABLED в конфигурационных файлах."""
    compose_path = Path(__file__).parent.parent / "docker-compose.yml"
    assert compose_path.exists()
    with open(compose_path, "r", encoding="utf-8") as f:
        compose_data = yaml.safe_load(f)

    worker_env = compose_data["services"]["vector-worker"]["environment"]
    assert any("CHROMA_ENVELOPE_ENCRYPTION_ENABLED" in e for e in worker_env)

    admin_env = compose_data["services"]["admin-server"]["environment"]
    assert any("CHROMA_ENVELOPE_ENCRYPTION_ENABLED" in e for e in admin_env)

    env_path = Path(__file__).parent.parent / ".env.example"
    assert env_path.exists()
    with open(env_path, "r", encoding="utf-8") as f:
        env_text = f.read()
    assert "CHROMA_ENVELOPE_ENCRYPTION_ENABLED" in env_text
