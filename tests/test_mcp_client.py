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
                        
    except Exception as e:
        print(f"❌ Ошибка подключения или выполнения: {e}")
        print("Убедись, что:")
        print("1. Контейнер mcp-rag запущен на VDS.")
        print("2. Порт 8001 открыт (или проброшен).")
        print("3. Ты указал правильный IP-адрес.")

if __name__ == "__main__":
    asyncio.run(main())
