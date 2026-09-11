import os
import io
from pdf2image import convert_from_path
from google.cloud import vision

def extract_text_cloud(pdf_path: str, pages_count: int = 1) -> str:
    """
    Извлекает текст из PDF-файла с использованием Google Cloud Vision API OCR.
    Конвертирует указанное количество страниц в изображения и отправляет их в API.
    Использует Application Default Credentials (ADC) для авторизации.
    
    Args:
        pdf_path (str): Путь к PDF-файлу.
        pages_count (int): Количество страниц для обработки, начиная с первой.
        
    Returns:
        str: Извлеченный текст.
    """
    try:
        # Определяет путь к poppler. Если poppler установлен через conda или находится в корне проекта.
        # Значение None позволяет pdf2image использовать системный PATH.
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        poppler_path = os.path.join(project_root, "poppler", "Library", "bin")
        if not os.path.exists(poppler_path):
            poppler_path = None
            
        images = convert_from_path(pdf_path, first_page=1, last_page=pages_count, poppler_path=poppler_path)
    except Exception as e:
        raise RuntimeError(f"Не удалось конвертировать PDF в изображения: {e}")

    client = vision.ImageAnnotatorClient()
    full_text = []

    for image in images:
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='JPEG')
        content = img_byte_arr.getvalue()
        
        vision_image = vision.Image(content=content)
        # Использование Google Vision OCR (не Document AI)
        response = client.document_text_detection(image=vision_image)
        
        if response.error.message:
            raise Exception(f"Ошибка Vision API: {response.error.message}")
            
        if response.full_text_annotation:
            full_text.append(response.full_text_annotation.text)

    return "\n\n".join(full_text)
