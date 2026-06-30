import os
from dotenv import load_dotenv
from litellm import completion

from tools.client_context import CLIENT_CONTEXT_SCHEMA, get_client_context
from tools.market_data import CBR_RATE_SCHEMA, get_cbr_key_rate

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
        "content": "Привет! Я твой клиент. Оцени мое текущее финансовое состояние и предложи консервативную стратегию, учитывая актуальную ключевую ставку ЦБ."
    })
    
    # Настраиваем ReAct цикл
    MAX_ITERATIONS = 5
    available_functions = {
        "get_client_context": get_client_context,
        "get_cbr_key_rate": get_cbr_key_rate
    }
    
    try:
        for iteration in range(MAX_ITERATIONS):
            print(f"\n[Итерация {iteration + 1}/{MAX_ITERATIONS}] Вызов LLM...")
            
            response = completion(
                model="vertex_ai/gemini-3.1-pro-preview",
                messages=messages,
                tools=[CLIENT_CONTEXT_SCHEMA, CBR_RATE_SCHEMA],
                vertex_project=os.getenv("VERTEX_PROJECT"),
                vertex_location=os.getenv("VERTEX_LOCATION")
            )
            message = response.choices[0].message
            
            # Проверяем, есть ли вызов иснтрументов
            if hasattr(message, "tool_calls") and message.tool_calls:
                print(f"[Модель запросила вызов инструментов: {len(message.tool_calls)} шт.]")
                
                # Сохраняем сообщение ассистента в историю
                messages.append(message.model_dump())
                
                # Обрабатываем каждый запрошенный вызов
                for tool_call in message.tool_calls:
                    function_name = tool_call.function.name
                    
                    if function_name in available_functions:
                        function_to_call = available_functions[function_name]
                        
                        # Вызываем функцию и получаем результат
                        function_result = function_to_call()
                        print(f" -> Выполнена функция: {function_name}")
                        
                        # Формируем сообщение с результатом и добавляем в историю
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": function_name,
                            "content": str(function_result)
                        })
                    else:
                        print(f" -> Неизвестная функция: {function_name}")
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": function_name,
                            "content": f"Ошибка: функция {function_name} не найдена"
                        })
                continue
            
            else:
                # Если вызова инструментов нет, значит модель сформулировала финальный ответ
                if message.content:
                    print(message.content)
                else:
                    print("[Ответ пуст]")
                break
                
        else:
            # Блок else для цикла for срабатывает, если цикл завершился без break
            print("\n[ВНИМАНИЕ] Агент превысил лимит шагов (MAX_ITERATIONS) и был принудительно остановлен.")
        
    except Exception as e:
        print(f"\nПроизошла ошибка: {e}")

if __name__ == "__main__":
    main()
