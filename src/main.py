import json
import sys
import os
import colorama
from dotenv import load_dotenv
from litellm import completion

sys.stdout.reconfigure(encoding='utf-8')
colorama.init()
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from tools.agent_tools.main_agent.client_context import CLIENT_CONTEXT_SCHEMA, get_client_context
from tools.agent_tools.main_agent.market_data import CBR_RATE_SCHEMA, get_cbr_key_rate
from tools.agent_tools.global_rag_search import KNOWLEDGE_BASE_SCHEMA, search_global_knowledge_base

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
        "content": "Привет! Я только что открыл брокерский счет, статуса квалифицированного инвестора у меня нет. Но я слышал про структурные облигации без защиты капитала, хочу вложить туда 500 тысяч рублей. Можешь проверить по нашей базе правил, разрешено ли мне покупать такие бумаги? Если нет, то какие условия или тесты мне нужно выполнить по закону?"
    })
    
    # Настраиваем ReAct цикл
    MAX_ITERATIONS = 10
    available_functions = {
        "get_client_context": get_client_context,
        "get_cbr_key_rate": get_cbr_key_rate,
        "search_global_knowledge_base": search_global_knowledge_base
    }
    
    # Запускаем ReAct цикл
    try:
        for iteration in range(MAX_ITERATIONS):
            print(f"System: \033[96m[Итерация]\033[0m Запуск шага {iteration + 1}/{MAX_ITERATIONS}")
            response = completion(
                model=os.getenv("MAIN_MODEL_NAME", "vertex_ai/gemini-3.1-pro-preview"),
                messages=messages,
                tools=[CLIENT_CONTEXT_SCHEMA, CBR_RATE_SCHEMA, KNOWLEDGE_BASE_SCHEMA],
                vertex_project=os.getenv("VERTEX_PROJECT"),
                vertex_location=os.getenv("VERTEX_LOCATION"),
                reasoning_effort=os.getenv("MAIN_REASONING_EFFORT", "high")
            )
            message = response.choices[0].message
            
            # Проверяем, есть ли вызов иснтрументов
            if hasattr(message, "tool_calls") and message.tool_calls:
                print(f"LLM: \033[95m[Запрос инструмента]\033[0m Модель запросила {len(message.tool_calls)} вызовов")
                # Сохраняем сообщение ассистента в историю
                messages.append(message.model_dump())
                
                # Обрабатываем каждый запрошенный вызов
                for tool_call in message.tool_calls:
                    function_name = tool_call.function.name
                    
                    if function_name in available_functions:
                        function_to_call = available_functions[function_name]
                        
                        # Парсим аргументы из запроса модели
                        try:
                            args = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
                        except Exception as e:
                            print(f"Logs: \033[91m[Ошибка]\033[0m Не удалось распарсить аргументы: {e}")
                            args = {}
                            
                        # Вызываем функцию с аргументами и получаем результат
                        print(f"Tool: \033[93m[Вызов]\033[0m {function_name} с аргументами {args}")
                        function_result = function_to_call(**args)
                        
                        # Формируем сообщение с результатом и добавляем в историю
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": function_name,
                            "content": str(function_result)
                        })
                    else:
                        print(f"Logs: \033[91m[Ошибка]\033[0m Неизвестная функция: {function_name}")
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
                    print(f"LLM: \033[92m[Ответ]\033[0m {message.content}")
                else:
                    print("LLM: \033[93m[Внимание]\033[0m Модель вернула пустой ответ")
                break
                
        else:
            # Блок else для цикла for срабатывает, если цикл завершился без break
            print("System: \033[93m[Предупреждение]\033[0m Превышен лимит итераций (MAX_ITERATIONS)")
        
    except Exception as e:
        print(f"Logs: \033[91m[Ошибка]\033[0m Произошла критическая ошибка: {e}")

if __name__ == "__main__":
    main()
