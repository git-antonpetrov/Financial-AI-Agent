import os
import subprocess
from src.utils.console_logger import log_error

def convert_to_pdf(input_path: str, output_dir: str) -> str:
    """
    Конвертирует документ (.txt, .md, .rtf, .doc, .docx) в формат PDF с помощью LibreOffice.
    Поддерживает как старый формат Word 97 (.doc), так и современные форматы.
    
    Args:
        input_path (str): Полный путь к исходному файлу.
        output_dir (str): Папка для сохранения готового PDF.
        
    Returns:
        str: Полный путь к сгенерированному PDF файлу.
        
    Raises:
        RuntimeError: В случае ошибки процесса LibreOffice.
        FileNotFoundError: Если итоговый файл не был создан.
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)
        
    soffice_path = "soffice"
    if os.path.exists(r"C:\Program Files\LibreOffice\program\soffice.exe"):
        soffice_path = r"C:\Program Files\LibreOffice\program\soffice.exe"

    command = [
        soffice_path,
        "--headless",
        "--convert-to",
        "pdf",
        input_path,
        "--outdir",
        output_dir
    ]
    
    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.decode('utf-8', errors='ignore')
        log_error("Конвертация в PDF", f"Ошибка LibreOffice при конвертации {input_path}: {error_msg}")
        raise RuntimeError(f"Ошибка конвертации {input_path} в PDF: {error_msg}")
        
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    expected_pdf = os.path.join(output_dir, f"{base_name}.pdf")
    
    if os.path.exists(expected_pdf):
        return expected_pdf
    else:
        log_error("Конвертация в PDF", f"PDF файл не был сгенерирован по пути {expected_pdf}")
        raise FileNotFoundError(f"PDF не был сгенерирован по пути {expected_pdf}")
