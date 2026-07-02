import os
# pyrefly: ignore [missing-import]
import chromadb

def get_chroma_client():
    """Инициализирует и возвращает клиент векторной базы данных."""
    return chromadb.HttpClient(host="localhost", port=8000)
