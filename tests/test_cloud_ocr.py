import os
import sys
import asyncio
import fitz  # PyMuPDF для подсчета страниц

# Добавляем корень проекта в sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.elt.utils.cloud_text_extractor import extract_text_cloud
from src.core.utils.console_logger import log_info, log_error

async def test_cloud_vision_ocr():
    """
    Уважаемый пользователь, данный скрипт тестирует работу облачного рекогнайзера Google Cloud Vision.
    Авторизация происходит автоматически через настройки вашего Google Cloud профиля.
    """
    log_info("Test OCR", "Добро пожаловать в тест Google Cloud Vision OCR.")
    
    test_pdf_path = os.path.join(os.path.dirname(__file__), "test_doc.pdf")
    
    if not os.path.exists(test_pdf_path):
        log_error("Test OCR", f"К сожалению, тестовый файл не найден: {test_pdf_path}")
        log_info("Test OCR", "Пожалуйста, положите любой PDF файл с именем 'test_doc.pdf' в папку tests/ и запустите тест снова.")
        return

    try:
        # Узнаем количество страниц в PDF
        doc = fitz.open(test_pdf_path)
        page_count = len(doc)
        doc.close()
        log_info("Test OCR", f"Замечательно, обнаружен PDF документ. Количество страниц: {page_count}.")
        
        log_info("Test OCR", "Начинаем извлечение текста, пожалуйста, подождите...")
        # Запускаем извлечение (в отдельном потоке, так как функция блокирующая)
        extracted_text = await asyncio.to_thread(extract_text_cloud, test_pdf_path, page_count)
        
        print("\n" + "="*50)
        print("РЕЗУЛЬТАТ РАСПОЗНАВАНИЯ:")
        print("="*50)
        # Выводим первые 2000 символов, чтобы не засорять консоль
        print(extracted_text[:2000] + ("\n... (текст обрезан)" if len(extracted_text) > 2000 else ""))
        print("="*50 + "\n")
        
        log_info("Test OCR", "Извлечение текста успешно завершено! Спасибо за использование нашего сервиса.")
        
    except Exception as e:
        log_error("Test OCR", f"Приношу свои извинения, произошла ошибка в процессе распознавания: {e}")

if __name__ == "__main__":
    asyncio.run(test_cloud_vision_ocr())
