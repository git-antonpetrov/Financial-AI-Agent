import os
import sys

# Добавляем корень проекта в sys.path, если скрипт запущен напрямую
if __name__ == "__main__":
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

# pyrefly: ignore [missing-import]
from mcp.server.mcpserver import MCPServer
from src.core.clients.vector_db import get_chroma_client
from src.core.clients.llm import get_embedder
from src.core.utils.console_logger import log_error, log_info

# Инициализируем MCPServer
mcp = MCPServer("RAG Search MCP", description="Сервер для поиска по нормативной базе знаний (ChromaDB)")

@mcp.tool()
def search_knowledge_base(query: str, max_results: int = 3) -> str:
    """
    Инструмент для семантического поиска по глобальной базе знаний.
    Используется для верификации финансовых стратегий, поиска по нормативно-правовым актам,
    предписаниям ЦБ РФ, регламентам Московской Биржи и аналитическим обзорам.
    
    Args:
        query: Поисковый запрос, например: 'активы для консервативного профиля правила ЦБ'
        max_results: Максимальное количество фрагментов текста (по умолчанию 3)
    """
    client = get_chroma_client()
    embedder = get_embedder()
    try:
        collection = client.get_collection(
            name="global_rules",
            embedding_function=embedder
        )
        results = collection.query(
            query_texts=[query],
            n_results=max_results
        )
        
        if results and results.get("documents") and len(results["documents"][0]) > 0:
            docs = results["documents"][0]
            metas = results.get("metadatas", [[{}]])[0]
            
            response_text = "ИНФОРМАЦИЯ ИЗ БАЗЫ ЗНАНИЙ:\n\n"
            for i, doc in enumerate(docs):
                meta = metas[i] if metas and i < len(metas) and metas[i] else {}
                source_name = meta.get("source", "Неизвестный источник")
                response_text += f"--- ФРАГМЕНТ {i+1} ---\nИсточник: {source_name}\nТекст: {doc}\n\n"
                
            return response_text.strip()
            
    except Exception as e:
        log_error("RAG MCP Search", f"Сбой поиска в базе: {e}") 
        return f"Ошибка при поиске в базе данных: {e}"
        
    return "Ничего релевантного в нормативной базе не найдено."

@mcp.tool()
def get_latest_documents(limit: int = 5) -> str:
    """
    Получает список недавно загруженных (самых новых) документов в базу знаний.
    Используется, когда нужно узнать, какие новые законы, указы или правила вышли.
    
    Args:
        limit: Количество документов для вывода (по умолчанию 5)
    """
    client = get_chroma_client()
    embedder = get_embedder()
    try:
        collection = client.get_collection(
            name="global_rules",
            embedding_function=embedder
        )
        # Получаем небольшую выборку, чтобы отфильтровать уникальные источники.
        results = collection.get(
            limit=limit * 20, # Берем с запасом, так как 1 документ разбит на много чанков
            include=["metadatas"]
        )
        
        if not results or not results.get("metadatas"):
            return "База документов пуста."
            
        unique_docs = set()
        response_text = "Последние загруженные документы в базу:\n\n"
        
        for meta in results["metadatas"]:
            source = meta.get("source", "Неизвестно")
            if source not in unique_docs:
                unique_docs.add(source)
                response_text += f"- {source}\n"
                if len(unique_docs) >= limit:
                    break
                    
        return response_text.strip()
        
    except Exception as e:
        log_error("RAG MCP Latest", f"Сбой получения документов: {e}") 
        return f"Ошибка при получении списка документов: {e}"

@mcp.tool()
def get_document_metadata(document_name: str) -> str:
    """
    Получает все метаданные для конкретного документа по его имени/источнику.
    Используется для уточнения дат, авторов и других тегов документа.
    
    Args:
        document_name: Имя документа (например, 'ukazanie_cb_rf_n_1001_y_2025.md')
    """
    client = get_chroma_client()
    embedder = get_embedder()
    try:
        collection = client.get_collection(
            name="global_rules",
            embedding_function=embedder
        )
        results = collection.get(
            where={"source": document_name},
            limit=1,
            include=["metadatas"]
        )
        
        if not results or not results.get("metadatas") or len(results["metadatas"]) == 0:
            return f"Документ с именем '{document_name}' не найден."
            
        meta = results["metadatas"][0]
        
        response_text = f"Метаданные документа '{document_name}':\n"
        for key, value in meta.items():
            response_text += f"- {key}: {value}\n"
            
        return response_text.strip()
        
    except Exception as e:
        log_error("RAG MCP Metadata", f"Сбой получения метаданных: {e}") 
        return f"Ошибка при получении метаданных: {e}"

if __name__ == "__main__":
    log_info("RAG MCP Server", "Запуск сервера RAG MCP на порту 8001...")
    mcp.run(transport="sse", port=8001)
