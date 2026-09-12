import os
import io
import fitz
from pdf2image import convert_from_path
from google.cloud import vision
from src.core.utils.console_logger import log_info, log_warning

def extract_text_cloud(pdf_path: str, pages_count: int = None) -> str:
    """
    Извлекает текст из PDF-файла.
    Сначала пытается использовать быстрый нативный парсер (PyMuPDF).
    Если текста извлечено слишком мало (вероятно, это скан), использует Google Cloud Vision API OCR
    в качестве резервного варианта.
    Извлекает ВЕСЬ текст документа (или до pages_count, если указано).
    
    Args:
        pdf_path (str): Путь к PDF-файлу.
        pages_count (int): Максимальное количество страниц для обработки (None - все страницы).
        
    Returns:
        str: Извлеченный текст.
    """
    text_content = ""
    try:
        # Пытаемся извлечь нативный текст с помощью PyMuPDF
        doc = fitz.open(pdf_path)
        total_pages = min(pages_count, len(doc)) if pages_count else len(doc)
        for i in range(total_pages):
            page = doc[i]
            text_content += page.get_text() + "\n"
        doc.close()
    except Exception as e:
        log_warning("Text Extractor", f"Ошибка PyMuPDF: {e}")

    # Если текста достаточно (не скан), возвращаем его
    if len(text_content.strip()) > total_pages * 50:  # в среднем более 50 символов на страницу
        log_info("Text Extractor", f"Успешно извлечен нативный текст ({len(text_content)} символов)")
        return text_content.strip()

    log_info("Text Extractor", "Текст не найден (вероятно, скан). Запуск OCR (Google Cloud Vision)...")
    
    # Fallback to OCR
    try:
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        poppler_path = os.path.join(project_root, "poppler", "Library", "bin")
        if not os.path.exists(poppler_path):
            poppler_path = None
            
        images = convert_from_path(pdf_path, first_page=1, last_page=pages_count, poppler_path=poppler_path, timeout=30)
    except Exception as e:
        raise RuntimeError(f"Не удалось конвертировать PDF в изображения: {e}")

    client = vision.ImageAnnotatorClient()
    full_text = []

    for image in images:
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='JPEG')
        content = img_byte_arr.getvalue()
        
        vision_image = vision.Image(content=content)
        response = client.document_text_detection(image=vision_image, timeout=30.0)
        
        if response.error.message:
            raise Exception(f"Ошибка Vision API: {response.error.message}")
            
        if response.full_text_annotation:
            full_text.append(response.full_text_annotation.text)

    return "\n\n".join(full_text)
