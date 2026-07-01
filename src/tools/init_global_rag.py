import os
import sys
import glob

sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
# pyrefly: ignore [missing-import]
from src.app_clients import AppClients
# pyrefly: ignore [missing-import]
from src.tools.metadata_generator import extract_document_title
# pyrefly: ignore [missing-import]
from langchain_text_splitters import RecursiveCharacterTextSplitter
# pyrefly: ignore [missing-import]
from langchain_community.document_loaders import (
    TextLoader,
    PyPDFLoader,
    Docx2txtLoader,
    UnstructuredWordDocumentLoader,
    UnstructuredODTLoader,
    UnstructuredMarkdownLoader,
)

def extract_text_from_file(filepath: str) -> str:
    ext = os.path.splitext(filepath)[1].lower()
    try:
        match ext:
            case '.txt':
                loader = TextLoader(filepath, encoding='utf-8')
            case '.pdf':
                loader = PyPDFLoader(filepath)
            case '.docx':
                loader = Docx2txtLoader(filepath)
            case '.doc':
                loader = UnstructuredWordDocumentLoader(filepath)
            case '.odt':
                loader = UnstructuredODTLoader(filepath)
            case '.md':
                loader = UnstructuredMarkdownLoader(filepath)
            case _:
                print(f"Logs: \033[93m[Предупреждение]\033[0m Формат {ext} не поддерживается: {filepath}")
                return ""
                
        docs = loader.load()
        return "\n".join(doc.page_content for doc in docs)
    except Exception as e:
        print(f"Logs: \033[91m[Ошибка]\033[0m Ошибка при чтении файла {filepath}: {e}")
        return ""

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
    
    # 3. Ищем файлы
    kb_dir = "knowledge_base"
    SUPPORTED_EXTENSIONS = ('.txt', '.md', '.pdf', '.docx', '.doc', '.odt')
    
    all_files = glob.glob(os.path.join(kb_dir, "*"))
    files_to_index = [f for f in all_files if os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS]
    
    if not files_to_index:
        print("Logs: \033[91m[Ошибка]\033[0m База знаний пуста (нет подходящих файлов)")
        return

    # 4. Настраиваем сплиттер
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=2000,
        chunk_overlap=300,
        length_function=len
    )
    
    total_chunks = 0
    
    for filepath in files_to_index:
        print(f"Logs: \033[96m[Процесс]\033[0m Читаем файл: {filepath}")
        text = extract_text_from_file(filepath)
            
        if not text.strip():
            print(f"Logs: \033[93m[Пропуск]\033[0m Файл {filepath} пуст")
            continue
            
        # Нарезаем на чанки
        chunks = text_splitter.split_text(text)
        filename = os.path.basename(filepath)
        source_title = extract_document_title(text, filename)
        
        documents = chunks
        ids = [f"{filename}_chunk_{i}" for i in range(len(chunks))]
        metadatas = [{"source": source_title} for _ in chunks]
        
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
