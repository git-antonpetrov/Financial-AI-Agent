import os
from src.core.app_clients import AppClients

async def extract_document_title(text: str, default_filename: str) -> str:
    text_snippet = text[:2000]
    prompt = (
        f"Ты системный анализатор документов. Твоя задача — прочитать начало текста "
        f"и вернуть только официальное название документа. Не используй кавычки, вводные слова "
        f"или форматирование. Если название определить невозможно, верни строку: {default_filename}.\n\n"
        f"Текст:\n{text_snippet}"
    )
    
    try:
        response = await AppClients.get_cloud_ai_client().acompletion(
            model=os.getenv("META_MODEL_NAME", "vertex_ai/gemini-3.5-flash"),
            messages=[{"role": "user", "content": prompt}],
            vertex_project=os.getenv("VERTEX_PROJECT"),
            vertex_location=os.getenv("VERTEX_LOCATION"),
            reasoning_effort=os.getenv("META_REASONING_EFFORT", "medium")
        )
        title = response.choices[0].message.content.strip()
        return title if title else default_filename
    except Exception as e:
        print(f"Logs: \033[93m[Предупреждение]\033[0m Ошибка генерации метаданных: {e}")
        return default_filename
