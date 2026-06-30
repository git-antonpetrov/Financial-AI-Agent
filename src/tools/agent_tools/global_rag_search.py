import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
# pyrefly: ignore [missing-import]
from src.app_clients import AppClients

def search_global_knowledge_base(query: str) -> str:
    """Выполняет семантический поиск по векторной базе знаний."""
    client = AppClients.get_chroma_db()
    embedder = AppClients.get_embedder_client()
    try:
        collection = client.get_collection(
            name="global_rules",
            embedding_function=embedder
        )
        results = collection.query(
            query_texts=[query],
            n_results=1
        )
        
        if results and results.get("documents") and len(results["documents"][0]) > 0:
            return f"НАЙДЕНО В БАЗЕ ЗНАНИЙ: {results['documents'][0][0]}"
            
    except Exception as e:
        print(f"Logs: \033[91m[Ошибка]\033[0m Сбой поиска в базе: {e}") 
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
