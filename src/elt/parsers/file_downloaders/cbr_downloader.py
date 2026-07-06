import os
import urllib.parse
from .base_downloader import BaseDownloader

class CbrDownloader(BaseDownloader):
    async def download(self, item):
        url = item['url']
        raw_title = item.get('title', 'Без_названия')
        safe_title = self._sanitize_filename(raw_title)
        
        async with self.sem:
            try:
                async with self.session.get(url) as response:
                    if response.status == 200:
                        ext = ""
                        cd = response.headers.get('Content-Disposition', '')
                        if 'filename=' in cd:
                            import re
                            fname_match = re.search(r'filename=["\']?([^"\';]+)["\']?', cd)
                            if fname_match:
                                parsed_name = urllib.parse.unquote(fname_match.group(1))
                                if "UTF-8''" in parsed_name:
                                    parsed_name = parsed_name.replace("UTF-8''", "")
                                if '.' in parsed_name:
                                    ext = '.' + parsed_name.split('.')[-1]
                                    
                        if not ext:
                            if '.' in url.split('/')[-1]:
                                ext = '.' + url.split('/')[-1].split('.')[-1].split('?')[0]
                            else:
                                ext = '.pdf' # Дефолт для ЦБ
                                
                        filename = safe_title + ext
                        filepath = os.path.join(self.landing_dir, filename)
                        
                        success = await self.save_file(response, filepath)
                        if success:
                            self.downloaded_count[0] += 1
                            print(f"\033[92m[CBR]\033[0m {self.downloaded_count[0]}/{self.total_count}: {filename} ... Успешно")
                            return None
                        else:
                            return f"File save error for {filename}"
                    else:
                        err = f"Статус {response.status}"
                        print(f"\033[91m[Ошибка CBR]\033[0m {err} для {url}")
                        return err
            except Exception as e:
                err = f"Исключение: {e}"
                print(f"\033[91m[Ошибка CBR]\033[0m {err} при скачивании {url}")
                return err
