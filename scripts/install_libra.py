import os
import sys
import subprocess
import colorama

sys.stdout.reconfigure(encoding='utf-8')
colorama.init()

def main():
    print(f"\033[96m[Установка LibreOffice]\033[0m Начинаем загрузку и установку через winget...")
    
    # Run the winget command
    command = [
        "winget", "install", "TheDocumentFoundation.LibreOffice",
        "--silent", "--accept-package-agreements", "--accept-source-agreements"
    ]
    
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='cp866', # windows console encoding
            errors='replace'
        )
        
        # Read output in real-time
        for line in process.stdout:
            print(line.strip())
            
        process.wait()
        
        if process.returncode == 0:
            print(f"\033[92m[Успех]\033[0m LibreOffice успешно установлен!")
        else:
            print(f"\033[93m[Внимание]\033[0m Установщик завершился с кодом {process.returncode}. Возможно, LibreOffice уже установлен.")
    except Exception as e:
        print(f"\033[91m[Ошибка]\033[0m Не удалось запустить winget: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
