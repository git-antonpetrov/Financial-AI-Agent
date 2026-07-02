import os
import urllib.request
import zipfile
import subprocess
import platform
import sys
sys.stdout.reconfigure(encoding='utf-8')

POPPLER_URL = "https://github.com/oschwartz10612/poppler-windows/releases/download/v24.08.0-0/Release-24.08.0-0.zip"
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TARGET_DIR = os.path.join(PROJECT_ROOT, 'poppler')
ZIP_PATH = os.path.join(PROJECT_ROOT, 'poppler.zip')

def install_poppler_windows():
    if os.path.exists(TARGET_DIR):
        print("[Windows] Poppler уже установлен.")
        return

    print("[Windows] Скачивание Poppler...")
    try:
        urllib.request.urlretrieve(POPPLER_URL, ZIP_PATH)
        
        print("[Windows] Распаковка Poppler...")
        with zipfile.ZipFile(ZIP_PATH, 'r') as zip_ref:
            zip_ref.extractall(PROJECT_ROOT)
        
        extracted_dir = os.path.join(PROJECT_ROOT, 'poppler-24.08.0')
        if os.path.exists(extracted_dir):
            os.rename(extracted_dir, TARGET_DIR)
            print(f"[Windows] Poppler установлен в {TARGET_DIR}")
        else:
            print("[Windows] Ошибка: не удалось найти распакованную папку poppler-24.08.0")
            
    except Exception as e:
        print(f"[Windows] Произошла ошибка при установке: {e}")
    finally:
        if os.path.exists(ZIP_PATH):
            os.remove(ZIP_PATH)

def install_poppler():
    system = platform.system()
    
    if system == "Windows":
        install_poppler_windows()
        
    elif system == "Linux":
        print("[Linux] Установка poppler-utils через пакетный менеджер apt...")
        try:
            subprocess.run("sudo apt-get update && sudo apt-get install -y poppler-utils", shell=True, check=True)
            print("[Linux] Poppler успешно установлен.")
        except subprocess.CalledProcessError as e:
            print(f"[Linux] Ошибка при выполнении apt. Убедитесь, что у вас есть права sudo и работает интернет. Детали: {e}")
        except Exception as e:
            print(f"[Linux] Непредвиденная ошибка при установке: {e}")
            
    elif system == "Darwin":
        print("[macOS] Установка poppler через Homebrew...")
        try:
            subprocess.run(["brew", "install", "poppler"], check=True)
            print("[macOS] Poppler успешно установлен.")
        except subprocess.CalledProcessError as e:
            print(f"[macOS] Ошибка при установке через Homebrew. Убедитесь, что brew работает. Детали: {e}")
        except FileNotFoundError:
            print("[macOS] Ошибка: Homebrew (утилита brew) не найдена. Пожалуйста, установите Homebrew сначала.")
        except Exception as e:
            print(f"[macOS] Непредвиденная ошибка при установке: {e}")
            
    else:
        print(f"[{system}] Операционная система не поддерживается автоматическим скриптом.")

if __name__ == "__main__":
    install_poppler()
