import os
from .base_downloader import BaseDownloader

class ExternalDownloader(BaseDownloader):
    async def download(self, item):
        url = item['url']
        raw_title = item.get('title', 'Без_названия')
        safe_title = self._sanitize_filename(raw_title)
        
        async with self.sem:
            try:
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
                async with self.session.get(url, headers=headers) as response:
                    if response.status == 200:
                        content_type = response.headers.get('Content-Type', '')
                        cd = response.headers.get('Content-Disposition', '')
                        
                        ext = '.html' # Дефолт для неизвестных ссылок (скорее всего это веб-страница)
                        
                        if 'pdf' in content_type.lower():
                            ext = '.pdf'
                        elif 'filename=' in cd:
                            import urllib.parse, re
                            fname_match = re.search(r'filename=["\']?([^"\';]+)["\']?', cd)
                            if fname_match:
                                parsed_name = urllib.parse.unquote(fname_match.group(1))
                                if '.' in parsed_name:
                                    ext = '.' + parsed_name.split('.')[-1]
                                    
                        filename = safe_title + ext
                        filepath = os.path.join(self.landing_dir, filename)
                        
                        success = await self.save_file(response, filepath)
                        if success:
                            self.downloaded_count[0] += 1
                            print(f"\033[92m[External]\033[0m {self.downloaded_count[0]}/{self.total_count}: {filename} ... Успешно")
                            return None
                        else:
                            return f"File save error for {filename}"
                    else:
                        err = f"Статус {response.status}"
                        print(f"\033[91m[Ошибка External]\033[0m {err} для {url}")
                        return err
            except Exception as e:
                err = f"Исключение: {e}"
                print(f"\033[91m[Ошибка External]\033[0m {err} при скачивании {url}")
                return err
