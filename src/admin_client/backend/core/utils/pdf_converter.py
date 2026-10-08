import os
import shutil
import tempfile
import subprocess
try:
    from src.common.logger import log_error
except ImportError:
    from core.utils.console_logger import log_error

def find_soffice_executable() -> str:
    """
    Находит исполняемый файл LibreOffice (soffice) в системе:
    1. Переменная окружения LIBREOFFICE_PATH или SOFFICE_PATH.
    2. Поиск в системном PATH (shutil.which).
    3. Стандартные пути установки на Windows (Program Files, Program Files x86, AppData).
    4. Стандартные пути на macOS и Linux.
    """
    # 1. Пользовательская переменная окружения
    custom_path = os.getenv("LIBREOFFICE_PATH") or os.getenv("SOFFICE_PATH")
    if custom_path and os.path.isfile(custom_path):
        return custom_path

    # 2. Поиск в системном PATH
    for candidate in ["soffice", "soffice.exe", "libreoffice"]:
        path = shutil.which(candidate)
        if path and os.path.isfile(path):
            return path

    # 3. Стандартные пути на Windows
    windows_candidates = [
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\LibreOffice\program\soffice.exe"),
    ]
    for win_path in windows_candidates:
        if os.path.isfile(win_path):
            return win_path

    # 4. Стандартные пути на Unix/macOS
    unix_candidates = [
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/usr/bin/soffice",
        "/usr/local/bin/soffice",
        "/usr/bin/libreoffice",
    ]
    for unix_path in unix_candidates:
        if os.path.isfile(unix_path):
            return unix_path

    raise FileNotFoundError(
        "LibreOffice (soffice) не найден в системе. Установите LibreOffice (https://www.libreoffice.org) "
        "или укажите путь к исполняемому файлу soffice через переменную окружения LIBREOFFICE_PATH."
    )

def convert_to_pdf(input_path: str, output_dir: str) -> str:
    """
    Конвертирует документ (.txt, .md, .rtf, .doc, .docx) в формат PDF с помощью LibreOffice.
    Поддерживает как старый формат Word 97 (.doc), так и современные форматы.
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)
        
    soffice_path = find_soffice_executable()

    user_profile_dir = tempfile.mkdtemp(prefix="lo_profile_")
    user_profile_uri = Path(user_profile_dir).as_uri()

    command = [
        soffice_path,
        f"-env:UserInstallation={user_profile_uri}",
        "--headless",
        "--convert-to",
        "pdf",
        input_path,
        "--outdir",
        output_dir
    ]
    
    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    except FileNotFoundError as e:
        log_error("Конвертация в PDF", f"Не удалось запустить LibreOffice по пути '{soffice_path}': {e}")
        raise FileNotFoundError(f"Не удалось запустить LibreOffice по пути '{soffice_path}': {e}") from e
    except subprocess.TimeoutExpired as e:
        log_error("Конвертация в PDF", f"Превышено время ожидания конвертации {input_path} (таймаут 120 сек)")
        raise TimeoutError(f"Превышено время ожидания LibreOffice при конвертации {input_path}") from e
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.decode('utf-8', errors='ignore')
        log_error("Конвертация в PDF", f"Ошибка LibreOffice при конвертации {input_path}: {error_msg}")
        raise RuntimeError(f"Ошибка конвертации {input_path} в PDF: {error_msg}")
    finally:
        shutil.rmtree(user_profile_dir, ignore_errors=True)
        
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    expected_pdf = os.path.join(output_dir, f"{base_name}.pdf")
    
    if os.path.exists(expected_pdf):
        return expected_pdf
    else:
        log_error("Конвертация в PDF", f"PDF файл не был сгенерирован по пути {expected_pdf}")
        raise FileNotFoundError(f"PDF не был сгенерирован по пути {expected_pdf}")
