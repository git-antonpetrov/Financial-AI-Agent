import os
import sys
import glob
import fitz

sys.stdout.reconfigure(encoding='utf-8')

def check_pdfs():
    storage_dir = "data/2_storage"
    pdf_files = glob.glob(os.path.join(storage_dir, "*.pdf"))
    
    total = len(pdf_files)
    broken_pdfs = []
    
    print(f"Найдено {total} PDF файлов для проверки в папке {storage_dir}.")
    
    for i, filepath in enumerate(pdf_files, 1):
        try:
            # Сначала проверяем бинарный заголовок (Magic Bytes)
            with open(filepath, 'rb') as f:
                header = f.read(1024)
                if b"%PDF-" not in header:
                    broken_pdfs.append(filepath)
                    continue

            # Затем проверяем, что он открывается как PDF и имеет страницы
            doc = fitz.open(filepath)
            if doc.page_count < 1:
                broken_pdfs.append(filepath)
            doc.close()
        except Exception as e:
            # Любая ошибка при открытии означает, что PDF битый
            broken_pdfs.append(filepath)
            
        # Логируем прогресс в консоль
        if i % 10 == 0 or i == total:
            print(f"Проверено: {i} / {total}")
            
    out_file = "broken_pdfs_report.txt"
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(f"Всего проверено файлов: {total}\n")
        f.write(f"Всего битых файлов: {len(broken_pdfs)}\n\n")
        if broken_pdfs:
            f.write("Список битых файлов:\n")
            for bad_file in broken_pdfs:
                f.write(f"{bad_file}\n")
        else:
            f.write("Битых файлов не обнаружено!\n")
            
    print(f"\nГотово! Найдено {len(broken_pdfs)} битых файлов.")
    print(f"Полный список сохранен в корень проекта: {out_file}")

if __name__ == "__main__":
    check_pdfs()
