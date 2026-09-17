import asyncio
import sys

# Важно для корректного вывода русского текста в Windows
sys.stdout.reconfigure(encoding='utf-8')

# pyrefly: ignore [missing-import]
from mcp import ClientSession
# pyrefly: ignore [missing-import]
from mcp.client.sse import sse_client

async def main():
    # TODO: ЗАМЕНИТЬ 127.0.0.1 НА IP-АДРЕС ТВОЕГО VDS (например, "http://192.168.1.100:8001/sse")
    # Если тестируешь всё же локально, оставь так.
    url = "http://135.106.211.209:8001/sse"
    
    print(f"Попытка подключения к MCP Серверу по адресу: {url}...")
    
    try:
        # Подключаемся по протоколу SSE
        async with sse_client(url) as streams:
            # Инициализируем сессию клиента
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                print("✅ Успешно подключено и инициализировано!\n")
                
                print("--- 🛠️ Доступные инструменты ---")
                tools_response = await session.list_tools()
                for tool in tools_response.tools:
                    print(f" - {tool.name}: {tool.description.strip().split(chr(10))[0]}...")
                
                print("\n--- 🔍 Тестируем инструмент search_knowledge_base ---")
                print("Делаем запрос: 'активы для профиля' (1 результат)...")
                
                # Вызываем инструмент
                result = await session.call_tool(
                    "search_knowledge_base", 
                    arguments={"query": "активы для профиля", "max_results": 1}
                )
                
                print("\nОтвет от сервера:")
                for content in result.content:
                    if content.type == "text":
                        print(content.text)
                        
                print("\n--- 📄 Тестируем инструмент get_latest_documents ---")
                result_docs = await session.call_tool(
                    "get_latest_documents", 
                    arguments={"limit": 3}
                )
                for content in result_docs.content:
                    if content.type == "text":
                        print(content.text)
                        
                print("\n--- 🌐 Тестируем инструмент search_internet_for_regulations ---")
                print("Делаем запрос: 'Ключевая ставка ЦБ РФ на сегодня' (max_results=2)...")
                try:
                    result_internet = await session.call_tool(
                        "search_internet_for_regulations", 
                        arguments={"query": "Ключевая ставка ЦБ РФ на сегодня", "max_results": 2}
                    )
                    for content in result_internet.content:
                        if content.type == "text":
                            print(content.text)
                except Exception as e:
                    print(f"Ошибка при вызове search_internet_for_regulations: {e}")

                print("\n--- 📝 Тестируем инструмент recover_clean_text_from_internet ---")
                print("Передаем фрагмент текста для восстановления...")
                mangled_text = "Банк России принял решение повысить ключевую ставку на 100 б.п., до 19,00% годовых. Инфляционное давление остается высоким. Были рассмотрены вопросы кредитно-денежной политики."
                try:
                    result_recover = await session.call_tool(
                        "recover_clean_text_from_internet", 
                        arguments={"mangled_snippet": mangled_text}
                    )
                    for content in result_recover.content:
                        if content.type == "text":
                            # Выводим только первые 1000 символов, чтобы не засорять консоль
                            print(content.text[:1000] + "\n\n[...остальной текст скрыт для краткости...]")
                except Exception as e:
                    print(f"Ошибка при вызове recover_clean_text_from_internet: {e}")
                        
    except Exception as e:
        print(f"❌ Ошибка подключения или выполнения: {e}")
        print("Убедись, что:")
        print("1. Контейнер mcp-rag запущен на VDS.")
        print("2. Порт 8001 открыт (или проброшен).")
        print("3. Ты указал правильный IP-адрес.")

if __name__ == "__main__":
    asyncio.run(main())
