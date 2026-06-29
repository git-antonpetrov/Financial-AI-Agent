import os
from dotenv import load_dotenv
from litellm import completion

# Загружаем переменные окружения из .env файла
load_dotenv()

def read_system_prompt(file_path: str) -> str:
    """Читает системный промпт из указанного файла."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        print(f"Файл системного промпта не найден по пути {file_path}.")
        return ""

def main():
    # Получаем системный промт
    prompt_path = "promts/main-agent-system-promt.md"
    system_prompt = read_system_prompt(prompt_path)
    
    # Собираем сообщение
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({
        "role": "user",
        "content": "Привет! Напомни, в чем заключается твоя главная задача?"
    })
    
    # Получаем ответ от LLM
    try:
        response = completion(
            model="vertex_ai/gemini-3.1-pro-preview",
            messages=messages,
            vertex_project=os.getenv("VERTEX_PROJECT"),
            vertex_location=os.getenv("VERTEX_LOCATION")
        )
        print(response.choices[0].message.content)
        
    except Exception as e:
        print(f"\nПроизошла ошибка: {e}")

if __name__ == "__main__":
    main()
