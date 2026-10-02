"""
Модуль интеграционного тестирования цепочки обработки документов DocumentPipeline.
"""

import sys
import os
import requests

# Подключает директорию backend клиентского приложения в sys.path
backend_path = os.path.join(os.path.dirname(__file__), '..', 'src', 'admin_client', 'backend')
sys.path.insert(0, backend_path)
for mod in list(sys.modules.keys()):
    if mod == 'core' or mod.startswith('core.'):
        del sys.modules[mod]

# pyrefly: ignore [missing-import]
from pipeline import DocumentPipeline


def get_admin_token(server_url: str) -> str:
    """Выполняет запрос к API для прохождения аутентификации и получения JWT."""
    login_url = f"{server_url}/login"
    admin_password = os.getenv("ADMIN_PASSWORD")
    if not admin_password:
        raise ValueError("Переменная окружения ADMIN_PASSWORD не задана")
    data = {
        "username": "admin",
        "password": admin_password
    }
    
    response = requests.post(login_url, data=data, timeout=10)
    response.raise_for_status()
    
    token = response.json().get("access_token")
    if not token:
        raise Exception("Токен не найден в ответе сервера")
        
    return token


def progress_callback(filename: str, status: str, message: str) -> None:
    """Выводит информационное сообщение о текущем статусе обработки файла."""
    print(f"[{status.upper()}] {filename} -> {message}")


def test_run():
    """Выполняет тестовый прогон обработки документа через локальный пайплайн."""
    server_url = "https://admin.fin-ai-agent.ru"
    
    try:
        # 1. Запрашивает токен администратора для авторизации запросов
        token = get_admin_token(server_url)
        
        # 2. Инициализирует экземпляр пайплайна с полученным токеном
        pipeline = DocumentPipeline(server_url, admin_token=token)
        pipeline.set_progress_callback(progress_callback)
        
        # 3. Определяет путь к тестовому документу
        test_file_path = os.path.join(os.path.dirname(__file__), 'test_doc.txt')
        
        # 4. Запускает обработку документа через пайплайн
        result = pipeline.process_file(test_file_path, agent_name="digital")
        assert result is not None
        
    except requests.exceptions.HTTPError as e:
        print(f"\nОшибка сервера ({e.response.status_code}): {e.response.text}")
    except requests.exceptions.RequestException as e:
        print(f"\nОшибка сети: {e}")
    except Exception as e:
        print(f"\nКритическая ошибка при тестировании: {e}")


if __name__ == "__main__":
    test_run()
