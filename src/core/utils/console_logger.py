import sys
import colorama

# Инициализирует colorama для корректной поддержки ANSI-цветов в терминале Windows.
colorama.init()

def _log(level_color: str, level_name: str, process_name: str, message: str) -> None:
    """
    Форматирует и выводит сообщение в консоль согласно заданному стандарту.
    
    Args:
        level_color (str): ANSI-код цвета для уровня логирования.
        level_name (str): Название уровня (Error, Warning, Info).
        process_name (str): Название процесса (например, "Конвертация в PDF").
        message (str): Текст сообщения.
    """
    reset = "\033[0m"
    process_color = "\033[94m" # Синий цвет для процесса
    
    print(f"{level_color}[{level_name}]{reset}{process_color}[{process_name}]{reset}: {message}")

def log_info(process_name: str, message: str) -> None:
    """Выводит информационное сообщение (зеленого цвета)."""
    # Зеленый цвет для Info
    _log("\033[92m", "Info", process_name, message)

def log_warning(process_name: str, message: str) -> None:
    """Выводит предупреждение (желтого цвета)."""
    # Желтый цвет для Warning
    _log("\033[93m", "Warning", process_name, message)

def log_error(process_name: str, message: str) -> None:
    """Выводит ошибку (красного цвета)."""
    # Красный цвет для Error
    _log("\033[91m", "Error", process_name, message)
