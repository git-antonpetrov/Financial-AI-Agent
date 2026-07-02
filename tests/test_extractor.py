import asyncio
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.stdout.reconfigure(encoding='utf-8')
from src.utils.text_extractor import UniversalExtractor

async def main():
    print("=== Testing UniversalExtractor ===")
    
    # 1. Текстовый файл (или RTF)
    rtf_file = "data/1_landing/Арбитражный процессуальный кодекс Российской Федерации.rtf"
    print(f"\n1. Чтение RTF: {rtf_file}")
    text_rtf = await UniversalExtractor.extract_text(rtf_file)
    print(f"Результат (первые 200 символов): {text_rtf[:200]}")
    print(f"Длина всего текста: {len(text_rtf)}")

    # 2. Нормальный PDF
    pdf_file = "data/1_landing/BankReg_01-07_2022.pdf"
    print(f"\n2. Чтение нормального PDF: {pdf_file}")
    text_pdf = await UniversalExtractor.extract_text(pdf_file, max_pages=1)
    print(f"Результат (первые 200 символов): {text_pdf[:200]}")
    print(f"Длина всего текста: {len(text_pdf)}")

    # 3. Слепой PDF (Имитация)
    # Выберем файл и жестко передадим его в _ocr_pdf
    print(f"\n3. Чтение PDF через OCR (Принудительно): {pdf_file}")
    text_ocr = await UniversalExtractor._ocr_pdf(pdf_file, max_pages=1)
    print(f"Результат (первые 200 символов): {text_ocr[:200]}")
    print(f"Длина всего текста: {len(text_ocr)}")
    
if __name__ == "__main__":
    asyncio.run(main())
