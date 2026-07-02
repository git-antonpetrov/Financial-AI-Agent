import os
import re

class BaseDownloader:
    def __init__(self, session, sem, landing_dir, downloaded_count, total_count):
        self.session = session
        self.sem = sem
        self.landing_dir = landing_dir
        self.downloaded_count = downloaded_count
        self.total_count = total_count
        
    def _sanitize_filename(self, title, max_length=150):
        safe_title = re.sub(r'[\\/*?:"<>|\n\r\t]', " ", title).strip()
        safe_title = re.sub(r'\s+', ' ', safe_title)
        if len(safe_title) > max_length:
            safe_title = safe_title[:max_length].strip() + "..."
        return safe_title
        
    async def save_file(self, response, filepath):
        if os.path.exists(filepath):
            return True
            
        try:
            with open(filepath, 'wb') as f:
                while True:
                    chunk = await response.content.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
            return True
        except Exception as e:
            print(f"\033[91m[Ошибка записи]\033[0m Ошибка сохранения файла {filepath}: {e}")
            return False

    async def download(self, item):
        """Метод, который переопределяется в наследниках"""
        raise NotImplementedError("Метод download должен быть переопределен")
