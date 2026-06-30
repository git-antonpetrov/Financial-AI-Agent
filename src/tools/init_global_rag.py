import os
import sys
import glob
import uuid

sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
# pyrefly: ignore [missing-import]
from src.app_clients import AppClients
# pyrefly: ignore [missing-import]
from langchain_text_splitters import RecursiveCharacterTextSplitter

def index_all_documents():
    print("Logs: \033[92m[Старт]\033[0m Начинаем индексацию базы знаний...")
    
    # 1. Получаем клиент ChromaDB
    client = AppClients.get_chroma_db()
    
    # 2. Инициализируем коллекцию
    embedder = AppClients.get_embedder_client()
    collection = client.get_or_create_collection(
        name="global_rules",
        embedding_function=embedder
    )
    
    # 3. Ищем текстовые файлы
    kb_dir = "knowledge_base"
    txt_files = glob.glob(os.path.join(kb_dir, "*.txt"))
    
    if not txt_files:
        print("Logs: \033[91m[Ошибка]\033[0m База знаний пуста (нет файлов)")
        return

    # 4. Настраиваем сплиттер
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=2000,
        chunk_overlap=300,
        length_function=len
    )
    
    total_chunks = 0
    
    for filepath in txt_files:
        print(f"Logs: \033[96m[Процесс]\033[0m Читаем файл: {filepath}")
        with open(filepath, "r", encoding="utf-8") as f:
            text = f.read()
            
        if not text.strip():
            print(f"Logs: \033[93m[Пропуск]\033[0m Файл {filepath} пуст")
            continue
            
        # Нарезаем на чанки
        chunks = text_splitter.split_text(text)
        
        documents = []
        ids = []
        metadatas = []
        
        for i, chunk in enumerate(chunks):
            documents.append(chunk)
            chunk_id = str(uuid.uuid4())
            ids.append(chunk_id)
            metadatas.append({"source": filepath, "chunk": i})
            
        # Загружаем в базу
        collection.upsert(
            documents=documents,
            ids=ids,
            metadatas=metadatas
        )
        total_chunks += len(chunks)
        print(f"Logs: \033[92m[Успех]\033[0m Файл {filepath} загружен ({len(chunks)} фрагментов)")

    print(f"Logs: \033[92m[Финиш]\033[0m Индексация завершена. Всего фрагментов: {total_chunks}")

if __name__ == "__main__":
    index_all_documents()
