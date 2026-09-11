import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from src.core.clients.vector_db import get_chroma_client
from src.core.clients.llm import get_embedder
from src.utils.console_logger import log_error

def search_global_knowledge_base(query: str) -> str:
    """Осуществляется семантический поиск по векторной базе знаний."""
    client = get_chroma_client()
    embedder = get_embedder()
    try:
        collection = client.get_collection(
            name="global_rules",
            embedding_function=embedder
        )
        results = collection.query(
            query_texts=[query],
            n_results=3
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
        log_error("Global RAG Search", f"Сбой поиска в базе: {e}") 
    return "Ничего релевантного в нормативной базе не найдено."

KNOWLEDGE_BASE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_global_knowledge_base",
        "description": "Инструмент для семантического поиска по глобальной базе знаний. Используется для верификации финансовых стратегий, поиска по нормативно-правовым актам, предписаниям ЦБ РФ, регламентам Московской Биржи и аналитическим обзорам.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Поисковый запрос, например: 'активы для консервативного профиля правила ЦБ'"
                }
            },
            "required": ["query"]
        }
    }
}
