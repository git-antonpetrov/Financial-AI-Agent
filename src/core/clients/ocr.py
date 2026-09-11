from functools import lru_cache
from src.services.content_ai_recognizer import ContentCaptureRecognizer
from src.utils.console_logger import log_info

@lru_cache
def get_content_ai_client() -> ContentCaptureRecognizer:
    """
    Возвращает экземпляр клиента для работы с Content AI (OCR).
    Использует кэширование для сохранения единственного экземпляра (Singleton).
    """
    log_info("OCR", "Инициализация Content AI...")
    return ContentCaptureRecognizer(delete_batch_after=True)
