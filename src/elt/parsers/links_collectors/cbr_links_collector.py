import os
import sys
import json
import asyncio
import aiohttp
import re
from bs4 import BeautifulSoup
import colorama

colorama.init()
sys.stdout.reconfigure(encoding='utf-8')

# Добавляем корень проекта в sys.path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))

# Пути к файлам (сохраняем в logs)
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
PARSERS_LOG_DIR = os.path.join(PROJECT_ROOT, "logs", "parsers")
ERRORS_LOG_DIR = os.path.join(PROJECT_ROOT, "logs", "errors")

os.makedirs(PARSERS_LOG_DIR, exist_ok=True)
os.makedirs(ERRORS_LOG_DIR, exist_ok=True)

LINKS_FILE = os.path.join(PARSERS_LOG_DIR, "parsed_cbr_links.json")
ERRORS_FILE = os.path.join(ERRORS_LOG_DIR, "get_link_errors.json")

BASE_URL = "https://cbr.ru"
START_URL = "https://cbr.ru/na/"

async def collect_links():
    print(f"\033[96m[Сбор ссылок]\033[0m Инициализация сбора с {START_URL}")
    
    collected_links = {} # url -> title
    errors = []
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(START_URL) as resp:
                text = await resp.text()
        except Exception as e:
            print(f"\033[91m[Ошибка]\033[0m Не удалось загрузить стартовую страницу: {e}")
            return None, None
            
        match = re.search(r'data-cross-ajax-url="([^"]+)"', text)
        if not match:
            print("\033[91m[Ошибка]\033[0m Не найден URL для пагинации на стартовой странице.")
            return None, None
            
        next_url = match.group(1).replace('&amp;', '&')
        page_num = 1
        
        print("\033[96m[Progress]\033[0m Начинаем парсинг страниц пагинации...")
        
        while next_url:
            full_url = BASE_URL + next_url
            try:
                async with session.get(full_url) as resp:
                    if resp.status != 200:
                        print(f"\033[93m[Предупреждение]\033[0m Сервер вернул статус {resp.status}. Остановка пагинации.")
                        break
                    text = await resp.text()
            except Exception as e:
                print(f"\033[91m[Ошибка]\033[0m Ошибка при запросе страницы {page_num}: {e}")
                break
                
            soup = BeautifulSoup(text, 'html.parser')
            results = soup.find_all('div', class_='cross-result')
            
            new_docs_count = 0
            empty_cards_count = 0
            
            for res in results:
                title_div = res.find('div', class_='title')
                date_number = res.find('div', class_='date-number')
                card_id = res.get('data-doc-id', 'Unknown')
                
                title_text = "Без названия"
                href = None
                
                if title_div:
                    a_tag = title_div.find('a')
                    # Если ссылки нет, берем просто текст из title
                    if a_tag:
                        href = a_tag.get('href')
                        title_text = a_tag.text.strip()
                    else:
                        title_text = title_div.text.strip()
                        
                if href:
                    if href.startswith('/'):
                        doc_url = BASE_URL + href
                    elif href.startswith('http'):
                        doc_url = href
                    else:
                        doc_url = BASE_URL + '/' + href
                        
                    if doc_url not in collected_links:
                        collected_links[doc_url] = title_text
                        new_docs_count += 1
                else:
                    # Карточка не содержит ссылки на файл
                    empty_cards_count += 1
                    
                    # Форматируем метаданные (например, номер и дату)
                    meta_info = date_number.text.strip() if date_number else "Нет доп. инфо"
                    # Убираем лишние пробелы и переносы
                    meta_info = re.sub(r'\s+', ' ', meta_info)
                    title_text = re.sub(r'\s+', ' ', title_text)
                    
                    error_info = {
                        "page_num": page_num,
                        "card_id": card_id,
                        "title": title_text,
                        "meta": meta_info,
                        "reason": "Отсутствует тег <a> с атрибутом href внутри блока title"
                    }
                    errors.append(error_info)
                    
            print(f"\033[96m[Page {page_num}]\033[0m Найдено ссылок: {new_docs_count}. Пустых карточек: {empty_cards_count}.")
            
            match = re.search(r'data-cross-ajax-url="([^"]+)"', text)
            if match:
                next_url = match.group(1).replace('&amp;', '&')
                page_num += 1
                await asyncio.sleep(0.2)
            else:
                print("\033[92m[Progress]\033[0m Пагинация завершена, больше страниц нет.")
                break
                
    final_links = [{"url": k, "title": v} for k, v in collected_links.items()]
    
    # Сохраняем ссылки
    with open(LINKS_FILE, 'w', encoding='utf-8') as f:
        json.dump(final_links, f, ensure_ascii=False, indent=4)
        
    # Сохраняем ошибки, если есть
    if errors:
        with open(ERRORS_FILE, 'w', encoding='utf-8') as f:
            json.dump(errors, f, ensure_ascii=False, indent=4)
            
    print(f"\n\033[92m[Завершено]\033[0m Всего уникальных ссылок: {len(final_links)}")
    print(f"\033[93m[Завершено]\033[0m Карточек без ссылок: {len(errors)}")
    print(f"Ссылки сохранены в {LINKS_FILE}")
    if errors:
        print(f"Ошибки сохранены в {ERRORS_FILE}")
        
    return final_links

if __name__ == '__main__':
    asyncio.run(collect_links())
