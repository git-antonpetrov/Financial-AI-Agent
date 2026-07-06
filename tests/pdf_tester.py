import os
import sys
import fitz  # PyMuPDF

# Fix Windows console unicode issues
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding='utf-8')

def test_pdfs(directory: str):
    pdf_files = []
    for root, dirs, files in os.walk(directory):
        for f in files:
            if f.lower().endswith('.pdf'):
                pdf_files.append(os.path.join(root, f))
                
    if not pdf_files:
        print(f"В указанной директории ({directory}) нет PDF файлов.")
        return
        
    print(f"Найдено {len(pdf_files)} PDF файлов. Начинаю проверку...")
    
    broken_files = []
    for i, file_path in enumerate(pdf_files, 1):
        try:
            doc = fitz.open(file_path)
            # Try to load the first page to ensure it's not totally broken
            if len(doc) > 0:
                doc.load_page(0)
            doc.close()
        except Exception as e:
            broken_files.append((file_path, str(e)))
            
        print(f"Проверено {i} из {len(pdf_files)}", end="\r")
        
    print(f"\nПроверено {len(pdf_files)} из {len(pdf_files)}")
    print("\n--- Результаты проверки ---")
    
    if not broken_files:
        print("Все файлы успешно открываются.")
    else:
        print(f"Найдено {len(broken_files)} битых файлов:")
        with open("broken_pdfs.txt", "w", encoding="utf-8") as out:
            for bf, err in broken_files:
                print(f"- {os.path.basename(bf)} (Ошибка: {err})")
                out.write(f"{bf}\n")
        print("Список битых файлов сохранен в broken_pdfs.txt в корне проекта.")

if __name__ == "__main__":
    target_dir = sys.argv[1] if len(sys.argv) > 1 else "data"
    if not os.path.exists(target_dir):
        print(f"Директория {target_dir} не существует.")
    else:
        test_pdfs(target_dir)
