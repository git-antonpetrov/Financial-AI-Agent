"""
Консольный логгер для клиентского приложения admin_client.
Использует централизованный src.common.logger при наличии в PYTHONPATH,
с автономным резервным механизмом при упаковке в standalone-бинарник.
"""

try:
    from src.common.logger import (
        log_info,
        log_success,
        log_warning,
        log_error,
    )
except ImportError:
    try:
        from common.logger import (
            log_info,
            log_success,
            log_warning,
            log_error,
        )
    except ImportError:
        import sys
        import colorama

        colorama.init(strip=not sys.stdout.isatty())

        def _log(level_color: str, level_name: str, process_name: str, message: str) -> None:
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
                pass

        def log_info(process_name: str, message: str) -> None:
            _log("\033[96m", "Info", process_name, message)

        def log_success(process_name: str, message: str) -> None:
            _log("\033[92m", "Success", process_name, message)

        def log_warning(process_name: str, message: str) -> None:
            _log("\033[93m", "Warning", process_name, message)

        def log_error(process_name: str, message: str) -> None:
            _log("\033[91m", "Error", process_name, message)

__all__ = [
    "log_info",
    "log_success",
    "log_warning",
    "log_error",
]
