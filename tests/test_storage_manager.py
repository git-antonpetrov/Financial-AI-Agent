import os
import shutil
import asyncio
import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

# Добавляем корень проекта в sys.path
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from src.etl.storage_manager import StorageManager

async def run_tests():
    print("\033[96m[Setup]\033[0m Очистка полигона...")
    landing_dir = "data/1_landing"
    storage_dir = "data/2_storage"
    tracker_file = "data/state_tracker.json"
    
    # Принудительная очистка
    if os.path.exists(landing_dir):
        shutil.rmtree(landing_dir)
    if os.path.exists(storage_dir):
        shutil.rmtree(storage_dir)
    if os.path.exists(tracker_file):
        os.remove(tracker_file)
        
    os.makedirs(landing_dir, exist_ok=True)
    os.makedirs(storage_dir, exist_ok=True)
    
    print("\033[96m[Setup]\033[0m Генерация фейковых данных...")
    file1_path = os.path.join(landing_dir, "random_123.txt")
    file2_path = os.path.join(landing_dir, "spam_doc.txt")
    file3_path = os.path.join(landing_dir, "complex_letter.txt")
    
    text1 = "Настоящее Указание 5969-У устанавливает требования..."
    text3 = "Информационное письмо Банка России об особенностях работы брокеров с неквалифицированными инвесторами от 12 мая 2023 года"
    
    # Создаем файлы
    with open(file1_path, "w", encoding="utf-8") as f:
        f.write(text1)
    with open(file2_path, "w", encoding="utf-8") as f:
        f.write(text1) # Дубликат Файла 1
    with open(file3_path, "w", encoding="utf-8") as f:
        f.write(text3)
        
    print("\033[96m[Execution]\033[0m Инициализация StorageManager...")
    manager = StorageManager()
    
    print("\n\033[93m--- Обработка Файла 1 (Ожидаем Regex) ---\033[0m")
    await manager.process_file(file1_path)
    
    print("\n\033[93m--- Обработка Файла 2 (Ожидаем удаление дубликата MD5) ---\033[0m")
    await manager.process_file(file2_path)
    
    print("\n\033[93m--- Обработка Файла 3 (Ожидаем LLM) ---\033[0m")
    await manager.process_file(file3_path)
    
    print("\n\033[96m[Results]\033[0m Содержимое state_tracker.json:")
    if os.path.exists(tracker_file):
        with open(tracker_file, "r", encoding="utf-8") as f:
            tracker_data = json.load(f)
            print(json.dumps(tracker_data, indent=4, ensure_ascii=False))
    else:
        print("Файл state_tracker.json не найден!")
        
    print("\n\033[96m[Teardown]\033[0m Уборка мусора...")
    if os.path.exists(landing_dir):
        shutil.rmtree(landing_dir)
    os.makedirs(landing_dir, exist_ok=True)
    
    if os.path.exists(storage_dir):
        shutil.rmtree(storage_dir)
    os.makedirs(storage_dir, exist_ok=True)
    
    with open(tracker_file, "w", encoding="utf-8") as f:
        json.dump({}, f)
        
    print("\033[92m[Teardown] Тестовая среда успешно очищена. Мусора не осталось.\033[0m")

if __name__ == "__main__":
    asyncio.run(run_tests())
