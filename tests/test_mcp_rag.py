import os
import sys
import json
import asyncio

sys.stdout.reconfigure(encoding='utf-8')
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

async def main():
    try:
        from src.mcp_servers.rag.server import mcp
        
        print("Успешно импортирован RAG MCP Server!")
        print(f"Имя сервера: {mcp.name}")
        
        print("\nЗарегистрированные инструменты:")
        tools = await mcp.list_tools()
        for tool in tools:
            print(f"\n[Инструмент]: {tool.name}")
            print(f"Описание: {tool.description}")
            print(f"Схема параметров: {json.dumps(tool.inputSchema, indent=2, ensure_ascii=False)}")
                
    except Exception as e:
        print(f"Ошибка при тестировании MCP сервера: {e}")

if __name__ == "__main__":
    asyncio.run(main())
