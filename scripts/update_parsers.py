import os
import sys
import argparse

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

# pyrefly: ignore [missing-import]
from src.utils.document_to_pdf import convert_to_pdf
# pyrefly: ignore [missing-import]
from src.services.content_ai_recognizer import ContentCaptureRecognizer
# pyrefly: ignore [missing-import]
from src.services.cloud_text_extractor import extract_text_cloud

def process_file(file_path: str, output_dir: str, engine: str):
    """Обрабатывает один файл (конвертация + распознавание)."""
    print(f"\n[{engine.upper()}] Обработка файла: {file_path}")
    
    # 1. Конвертация в PDF (если необходимо)
    ext = os.path.splitext(file_path)[1].lower()
    working_pdf_path = file_path
    
    if ext in ['.doc', '.docx', '.rtf', '.odt']:
        print("Требуется конвертация в PDF...")
        working_pdf_path = convert_to_pdf(file_path, output_dir)
        print(f"Сконвертировано: {working_pdf_path}")
    elif ext != '.pdf':
        print(f"Пропуск неподдерживаемого формата: {ext}")
        return

    # 2. Распознавание
    results = {}
    if engine == "content_ai":
        recognizer = ContentCaptureRecognizer()
        results = recognizer.recognize(working_pdf_path)
    elif engine == "vision":
        # Vision возвращает просто строку, завернем её в словарь для единообразия
        text = extract_text_cloud(working_pdf_path)
        results = {os.path.basename(working_pdf_path): text}
    else:
        print(f"Неизвестный движок: {engine}")
        return

    # 3. Сохранение результатов
    os.makedirs(output_dir, exist_ok=True)
    base_name = os.path.basename(file_path)
    
    for key, content in results.items():
        # Если это XML из Content AI, сохраняем как .xml, иначе как .txt
        out_ext = ".xml" if engine == "content_ai" else ".txt"
        out_path = os.path.join(output_dir, f"{base_name}_{key}{out_ext}")
        
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(content)
            
        print(f"Сохранен результат: {out_path}")

def main():
    parser = argparse.ArgumentParser(description="Скрипт массовой конвертации и распознавания документов.")
    parser.add_argument("--input", "-i", type=str, required=True, help="Путь к файлу или папке с документами")
    parser.add_argument("--output", "-o", type=str, default="data/parsed", help="Папка для сохранения результатов")
    parser.add_argument("--engine", "-e", choices=["content_ai", "vision"], default="content_ai", help="Движок распознавания")
    
    args = parser.parse_args()
    
    if os.path.isfile(args.input):
        process_file(args.input, args.output, args.engine)
    elif os.path.isdir(args.input):
        for root, _, files in os.walk(args.input):
            for file in files:
                process_file(os.path.join(root, file), args.output, args.engine)
    else:
        print(f"Путь не найден: {args.input}")

if __name__ == "__main__":
    main()
