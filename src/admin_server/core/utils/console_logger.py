import sys
import colorama

# Инициализирует colorama. strip=not isatty() предотвращает удаление ANSI-кодов,
# если вывод перенаправлен в pipe (например, при запуске из Tauri sidecar),
# и предотвращает ошибки при записи.
colorama.init(strip=not sys.stdout.isatty())

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
    msg = f"{level_color}[{level_name}]{reset}{process_color}[{process_name}]{reset}: {message}"
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        # Переключаемся на ascii с заменой символов, чтобы избежать ошибок charmap или Errno 22 (pipe)
        safe_msg = msg.encode('ascii', 'replace').decode('ascii')
        try:
            print(safe_msg, flush=True)
        except Exception:
            pass
    except OSError:
        # Перехватываем Errno 22 Invalid argument или ошибки сломанного пайпа в Tauri
        pass

def log_info(process_name: str, message: str) -> None:
    """Выводит информационное сообщение (голубого цвета)."""
    # Голубой цвет для Info
    _log("\033[96m", "Info", process_name, message)

def log_success(process_name: str, message: str) -> None:
    """Выводит сообщение об успехе (зеленого цвета)."""
    # Зеленый цвет для Success
    _log("\033[92m", "Success", process_name, message)

def log_warning(process_name: str, message: str) -> None:
    """Выводит предупреждение (желтого цвета)."""
    # Желтый цвет для Warning
    _log("\033[93m", "Warning", process_name, message)

def log_error(process_name: str, message: str) -> None:
    """Выводит ошибку (красного цвета)."""
    # Красный цвет для Error
    _log("\033[91m", "Error", process_name, message)
