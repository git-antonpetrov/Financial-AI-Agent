import os
import sys
import asyncio
sys.stdout.reconfigure(encoding='utf-8')
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from src.core.app_clients import AppClients
async def test_gemini():
    print("Инициализация клиента Cloud AI...")
    client = AppClients.get_cloud_ai_client()
    model_name = os.getenv("PIPELINE_MODEL_NAME", "vertex_ai/gemini-3.5-flash")
    reasoning_effort = os.getenv("PIPELINE_REASONING_EFFORT", "medium")
    print(f"Тестируем модель: {model_name} (уровень: {reasoning_effort})")
    messages = [
        {"role": "user", "content": "Напиши короткое приветствие для тестирования API. Одно предложение."}
    ]
    try:
        print("Отправка запроса через litellm...")
        response = await client.acompletion(
            model=model_name,
            messages=messages,
            reasoning_effort=reasoning_effort
        )
        print("\nУСПЕХ! Ответ от Gemini:")
        print(f"\033[92m{response.choices[0].message.content}\033[0m")
    except Exception as e:
        print(f"\n\033[91mОШИБКА: {str(e)}\033[0m")
        import traceback
        traceback.print_exc()
if __name__ == "__main__":
    asyncio.run(test_gemini())
