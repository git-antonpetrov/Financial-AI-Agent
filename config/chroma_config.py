import os
# pyrefly: ignore [missing-import]
import chromadb

def get_chroma_client():
    """Инициализирует и возвращает клиент векторной базы данных."""
    db_path = "data/chroma_db"
    os.makedirs(db_path, exist_ok=True)
    return chromadb.PersistentClient(path=db_path)
