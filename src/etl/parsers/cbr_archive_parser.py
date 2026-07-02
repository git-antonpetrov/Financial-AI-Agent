import os
import sys
import json
import asyncio
import aiohttp
import urllib.parse
import colorama

colorama.init()
sys.stdout.reconfigure(encoding='utf-8')
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from src.etl.parsers.file_downloaders import CbrDownloader, PravoDownloader, ExternalDownloader
from src.etl.parsers.links_collectors.cbr_links_collector import collect_links, LINKS_FILE

LANDING_DIR = "data/1_landing"

async def route_and_download(session, item, sem, downloaded_count, total_count, errors_list):
    url = item['url']
    parsed_url = urllib.parse.urlparse(url)
    domain = parsed_url.netloc.lower()
    
    match domain:
        case "cbr.ru" | "www.cbr.ru":
            downloader = CbrDownloader(session, sem, LANDING_DIR, downloaded_count, total_count)
        case "publication.pravo.gov.ru" | "pravo.gov.ru" | "www.pravo.gov.ru" | "www.pravo.gov.ruhttp" | "pravo.gov.ru:8080":
            downloader = PravoDownloader(session, sem, LANDING_DIR, downloaded_count, total_count)
        case _:
            downloader = ExternalDownloader(session, sem, LANDING_DIR, downloaded_count, total_count)
            
    err = await downloader.download(item)
    if err:
        errors_list.append({
            "url": item["url"],
            "title": item.get("title", "Без названия"),
            "error": err
        })

async def main():
    os.makedirs(LANDING_DIR, exist_ok=True)
    
    links = []
    
    if os.path.exists(LINKS_FILE):
        print(f"\033[96m[Инфо]\033[0m Найден файл {LINKS_FILE}. Загружаем ссылки...")
        with open(LINKS_FILE, 'r', encoding='utf-8') as f:
            links = json.load(f)
    
    if not links:
        print(f"\033[93m[Инфо]\033[0m Файл со ссылками не найден или пуст. Авто-запуск сборщика...")
        links = await collect_links()
        
    if not links:
        print("\033[91m[Ошибка]\033[0m Нет ссылок для скачивания.")
        return
        
    print(f"\n\033[96m[Оркестратор]\033[0m Запуск скачивания {len(links)} документов...")
    
    sem = asyncio.Semaphore(10)
    downloaded_count = [0]
    total_count = len(links)
    errors_list = []
    
    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [route_and_download(session, item, sem, downloaded_count, total_count, errors_list) for item in links]
        await asyncio.gather(*tasks)
        
    # Сохранение отчета об ошибках
    errors_dir = os.path.join(os.path.dirname(__file__), "errors")
    os.makedirs(errors_dir, exist_ok=True)
    errors_file = os.path.join(errors_dir, "cbr_archive_parser_errors.json")
    
    report = {
        "summary": f"Скачано {downloaded_count[0]} из {total_count}. Ошибок: {len(errors_list)}",
        "errors": errors_list
    }
    
    with open(errors_file, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=4)
        
    print(f"\n\033[92m[Завершение]\033[0m {report['summary']}")
    print(f"\033[96m[Лог]\033[0m Отчет сохранен в {errors_file}")

if __name__ == '__main__':
    asyncio.run(main())
