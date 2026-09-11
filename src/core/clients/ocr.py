from functools import lru_cache
from src.elt.utils.content_ai_recognizer import ContentCaptureRecognizer
from src.core.utils.console_logger import log_info

@lru_cache
def get_content_ai_client() -> ContentCaptureRecognizer:
    """
    Возвращает экземпляр клиента для работы с Content AI (OCR).
    Использует кэширование для сохранения единственного экземпляра (Singleton).
    """
    log_info("OCR", "Инициализация Content AI...")
    return ContentCaptureRecognizer(delete_batch_after=True)
