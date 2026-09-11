import os
import sys
import asyncio

# Добавляем корень проекта в sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.clients.llm import get_cloud_ai_client
from src.core.config import get_settings
from src.core.utils.console_logger import log_info, log_error

async def test_llm_connection():
    """
    Скрипт для тестирования соединения с Vertex AI (включая работу через прокси).
    """
    settings = get_settings()
    log_info("Test LLM", "Инициализация клиента LLM...")
    
    if settings.VERTEX_API_BASE:
        log_info("Test LLM", f"Шлюз (прокси) АКТИВЕН: {settings.VERTEX_API_BASE}")
    else:
        log_info("Test LLM", "Шлюз НЕ активен. Запросы пойдут напрямую.")

    try:
        client = get_cloud_ai_client()
        log_info("Test LLM", "Отправка тестового запроса к модели Gemini...")
        
        response = await client.acompletion(
            model=settings.PIPELINE_MODEL_NAME,
            messages=[{"role": "user", "content": "Привет! Скажи 'Связь установлена', если ты меня слышишь."}],
            max_tokens=500
        )
        
        print(f"RAW RESPONSE: {response}")
        
        if not response.choices:
            log_error("Test LLM", "Получен пустой список choices от API.")
            return

        answer = response.choices[0].message.content
        print("\n" + "="*50)
        print("ОТВЕТ ОТ НЕЙРОСЕТИ:")
        print("="*50)
        print(answer)
        print("="*50 + "\n")
        
        log_info("Test LLM", "Тест успешно завершен!")
        
    except Exception as e:
        log_error("Test LLM", f"Ошибка при выполнении запроса: {e}")

if __name__ == "__main__":
    asyncio.run(test_llm_connection())
