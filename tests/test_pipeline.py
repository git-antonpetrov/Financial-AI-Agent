import sys
import os
import requests

# Добавляем путь к backend, чтобы импорты из pipeline.py работали
backend_path = os.path.join(os.path.dirname(__file__), '..', 'src', 'admin_client', 'backend')
sys.path.insert(0, backend_path)
for mod in list(sys.modules.keys()):
    if mod == 'core' or mod.startswith('core.'):
        del sys.modules[mod]

# pyrefly: ignore [missing-import]
from pipeline import DocumentPipeline

def get_admin_token(server_url: str) -> str:
    """Делает реальный запрос к API для авторизации и получения JWT"""
    login_url = f"{server_url}/login"
    # Для OAuth2PasswordRequestForm данные передаются как x-www-form-urlencoded
    admin_password = os.getenv("ADMIN_PASSWORD")
    if not admin_password:
        raise ValueError("Переменная окружения ADMIN_PASSWORD не задана")
    data = {
        "username": "admin",
        "password": admin_password
    }
    
    print(f"Авторизация на сервере: {login_url}...")
    response = requests.post(login_url, data=data, timeout=10)
    response.raise_for_status()
    
    token = response.json().get("access_token")
    if not token:
        raise Exception("Токен не найден в ответе сервера")
        
    return token

def progress_callback(filename, status, message):
    print(f"[{status.upper()}] {filename} -> {message}")

def test_run():
    print("=== ЗАПУСК ТЕСТОВОГО ПРОГОНА ПАЙПЛАЙНА ===")
    
    server_url = "https://admin.fin-ai-agent.ru"
    
    try:
        # 1. Запрашиваем токен (как это будет делать фронтенд)
        token = get_admin_token(server_url)
        print("Токен успешно получен от сервера!")
        
        # 2. Инициализируем пайплайн
        pipeline = DocumentPipeline(server_url, admin_token=token)
        pipeline.set_progress_callback(progress_callback)
        
        # 3. Путь к нашему тестовому файлу
        test_file_path = os.path.join(os.path.dirname(__file__), 'test_doc.txt')
        
        # 4. Запускаем обработку
        print("\nНачинаем прогон файла через пайплайн...")
        result = pipeline.process_file(test_file_path, agent_name="digital")
        
        print("\n=== РЕЗУЛЬТАТ ===")
        import json
        print(json.dumps(result, indent=2, ensure_ascii=False))
        
    except requests.exceptions.HTTPError as e:
        print(f"\nОшибка сервера ({e.response.status_code}): {e.response.text}")
    except requests.exceptions.RequestException as e:
        print(f"\nОшибка сети: {e}")
    except Exception as e:
        print(f"\nКритическая ошибка при тестировании: {e}")

if __name__ == "__main__":
    test_run()
