import os
import re
from .base_downloader import BaseDownloader

class PravoDownloader(BaseDownloader):
    async def download(self, item):
        original_url = item['url']
        raw_title = item.get('title', 'Без_названия')
        safe_title = self._sanitize_filename(raw_title)
        
        pdf_url = original_url
        expected_ext = '.html'
        
        # Пытаемся вытащить ID документа и подставить в API получения PDF
        # Примеры: http://publication.pravo.gov.ru/document/0001202511280088
        match = re.search(r'/(\d{10,25})', original_url)
        if match:
            doc_id = match.group(1)
            pdf_url = f"http://publication.pravo.gov.ru/file/pdf?eoNumber={doc_id}"
            expected_ext = '.pdf'
        elif 'proxy/ips' in original_url:
            # Для ИПС Право (веб-версия) генерируем прямую ссылку на экспорт в RTF
            pdf_url = original_url.replace('?docbody=', '?savertf=')
            if '&page=all' not in pdf_url:
                pdf_url += '&page=all'
            expected_ext = '.rtf'
            
        async with self.sem:
            try:
                # Право.gov часто требует User-Agent
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
                async with self.session.get(pdf_url, headers=headers) as response:
                    if response.status == 200:
                        content_type = response.headers.get('Content-Type', '')
                        
                        if 'pdf' in content_type.lower() or 'pdf' in pdf_url.lower():
                            ext = '.pdf'
                        elif 'rtf' in content_type.lower() or 'rtf' in pdf_url.lower():
                            ext = '.rtf'
                        else:
                            ext = expected_ext
                            
                        filename = safe_title + ext
                        filepath = os.path.join(self.landing_dir, filename)
                        
                        success = await self.save_file(response, filepath)
                        if success:
                            self.downloaded_count[0] += 1
                            print(f"\033[92m[Pravo]\033[0m {self.downloaded_count[0]}/{self.total_count}: {filename} ... Успешно")
                            return None
                        else:
                            return f"File save error for {filename}"
                    else:
                        err = f"Статус {response.status} (PDF API: {pdf_url})"
                        print(f"\033[91m[Ошибка Pravo]\033[0m {err} для {original_url}")
                        return err
            except Exception as e:
                err = f"Исключение: {e}"
                print(f"\033[91m[Ошибка Pravo]\033[0m {err} при скачивании {original_url}")
                return err
