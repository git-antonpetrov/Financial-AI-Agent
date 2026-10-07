"""
Модуль стандартизированного консольного логирования для микросервисов и клиентских приложений.
Обеспечивает цветной ANSI-вывод, потокобезопасность и отказоустойчивость при работе с не-TTY пайпами.
"""

import sys
import colorama

# Инициализация colorama. strip=not isatty() предотвращает некорректное удаление ANSI-кодов
# при перенаправлении вывода в pipe (например, в Tauri sidecar или Docker log driver)
colorama.init(strip=not sys.stdout.isatty())


def _log(level_color: str, level_name: str, process_name: str, message: str) -> None:
    """
    Форматирует и выводит сообщение в консоль согласно корпоративному стандарту.

    Args:
        level_color: ANSI-код цвета уровня логирования.
        level_name: Название уровня логирования (Error, Warning, Info, Success).
        process_name: Идентификатор подсистемы или процесса.
        message: Текст сообщения.
    """
    reset = "\033[0m"
    process_color = "\033[94m"
    msg = f"{level_color}[{level_name}]{reset}{process_color}[{process_name}]{reset}: {message}"
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        safe_msg = msg.encode("ascii", "replace").decode("ascii")
        try:
            print(safe_msg, flush=True)
        except Exception:
            pass
    except OSError:
        # Перехват Errno 22 (Invalid argument) или BrokenPipeError при работе с межпроцессными пайпами
        pass


def log_info(process_name: str, message: str) -> None:
    """Выводит информационное сообщение (голубой цвет)."""
    _log("\033[96m", "Info", process_name, message)


def log_success(process_name: str, message: str) -> None:
    """Выводит сообщение об успешном завершении операции (зеленый цвет)."""
    _log("\033[92m", "Success", process_name, message)


def log_warning(process_name: str, message: str) -> None:
    """Выводит предупреждение (желтый цвет)."""
    _log("\033[93m", "Warning", process_name, message)


def log_error(process_name: str, message: str) -> None:
    """Выводит сообщение об ошибке (красный цвет)."""
    _log("\033[91m", "Error", process_name, message)
