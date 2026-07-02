import os
import sys
import json
import asyncio
import base64
import logging
from io import BytesIO
from typing import Optional, List, Tuple
from dotenv import load_dotenv

# Libraries
import pypdf
import docx2txt
from striprtf.striprtf import rtf_to_text
from pdf2image import convert_from_path
import olefile
import re
import litellm
from litellm import acompletion
litellm.drop_params = True

load_dotenv()

# Setup logging
os.makedirs("src/utils/errors", exist_ok=True)
logger = logging.getLogger("UniversalExtractor")
logger.setLevel(logging.INFO)
# Console handler
ch = logging.StreamHandler(sys.stdout)
ch.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(ch)

CACHE_DIR = "data/.text_cache"
os.makedirs(CACHE_DIR, exist_ok=True)

class UniversalExtractor:
    # Poppler path for Windows (downloaded to root/poppler)
    _POPPLER_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'poppler', 'Library', 'bin'))
    
    @classmethod
    def _get_cache_path(cls, filepath: str, max_pages: Optional[int] = None) -> str:
        base_name = os.path.basename(filepath)
        if max_pages is not None:
            cache_name = f"{base_name}_pages_{max_pages}.txt"
        else:
            cache_name = f"{base_name}_full.txt"
        return os.path.join(CACHE_DIR, cache_name)
    
    @classmethod
    def _log_error(cls, filepath: str, reason: str, page_num: Optional[int] = None):
        error_dir = os.path.join("logs", "errors")
        os.makedirs(error_dir, exist_ok=True)
        error_file = os.path.join(error_dir, "text_extractor_errors.json")
        error_entry = {
            "file": filepath,
            "page": page_num,
            "reason": reason
        }
        logger.error(f"Failed extracting {filepath}: {reason}")
        
        try:
            if os.path.exists(error_file):
                with open(error_file, 'r', encoding='utf-8') as f:
                    errors = json.load(f)
            else:
                errors = []
            errors.append(error_entry)
            with open(error_file, 'w', encoding='utf-8') as f:
                json.dump(errors, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"Could not write to error log: {e}")

    @classmethod
    async def extract_text(cls, filepath: str, max_pages: Optional[int] = None) -> str:
        cache_path = cls._get_cache_path(filepath, max_pages)
        if os.path.exists(cache_path):
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    return f.read()
            except Exception as e:
                logger.warning(f"Failed to read cache {cache_path}: {e}")
                
        ext = os.path.splitext(filepath)[1].lower()
        result_text = ""
        
        try:
            if ext in ['.txt', '.md']:
                with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                    result_text = f.read()
            elif ext == ".docx":
                result_text = docx2txt.process(filepath)
            elif ext == ".doc":
                result_text = await cls._extract_doc(filepath)
            elif ext == ".odt":
                result_text = await cls._extract_odt(filepath)
            elif ext == '.rtf':
                with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                result_text = rtf_to_text(content)
            elif ext == '.pdf':
                result_text = await cls._extract_pdf(filepath, max_pages)
            else:
                logger.warning(f"Unsupported extension {ext} for file {filepath}")
                return ""
                
        except Exception as e:
            cls._log_error(filepath, str(e))
            return ""
            
        if result_text and result_text.strip():
            try:
                with open(cache_path, 'w', encoding='utf-8') as f:
                    f.write(result_text)
            except Exception as e:
                logger.error(f"Could not save cache to {cache_path}: {e}")
                
        return result_text

    @classmethod
    async def _extract_pdf(cls, filepath: str, max_pages: Optional[int]) -> str:
        # Step 1: Attempt local extraction
        text = ""
        try:
            reader = pypdf.PdfReader(filepath)
            pages_to_read = len(reader.pages)
            if max_pages is not None:
                pages_to_read = min(pages_to_read, max_pages)
                
            for i in range(pages_to_read):
                page_text = reader.pages[i].extract_text()
                if page_text:
                    text += page_text + "\n"
        except Exception as e:
            logger.warning(f"pypdf could not read {filepath}: {e}")
            
        if len(text.strip()) >= 1000:
            return text
            
        # Step 2: Fallback to OCR
        logger.info(f"PDF {filepath} seems to be a blind scan (text length < 1000). Falling back to OCR.")
        return await cls._ocr_pdf(filepath, max_pages)

    @classmethod
    async def _ocr_pdf(cls, filepath: str, max_pages: Optional[int]) -> str:
        if not os.path.exists(cls._POPPLER_PATH):
            msg = f"Poppler not found at {cls._POPPLER_PATH}."
            cls._log_error(filepath, msg)
            return ""
            
        try:
            # Note: convert_from_path takes last_page parameter (1-indexed)
            images = convert_from_path(filepath, poppler_path=cls._POPPLER_PATH, last_page=max_pages)
        except Exception as e:
            cls._log_error(filepath, f"pdf2image conversion failed: {e}")
            return ""

        if not images:
            return ""

        # Batch images by 3
        batch_size = 3
        batches = []
        for i in range(0, len(images), batch_size):
            batches.append((i, images[i:i+batch_size]))

        semaphore = asyncio.Semaphore(15)
        
        async def process_batch(start_idx: int, batch_imgs: List) -> Tuple[int, str]:
            async with semaphore:
                return await cls._ocr_batch(start_idx, batch_imgs, filepath)

        tasks = [process_batch(idx, imgs) for idx, imgs in batches]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        valid_results = []
        for res in results:
            if isinstance(res, Exception):
                logger.error(f"OCR batch failed: {res}")
            elif res[1]:  # if text is not empty
                valid_results.append(res)
                
        # Sort rigidly by index to prevent race conditions
        valid_results.sort(key=lambda x: x[0])
        
        final_text = " ".join(text for _, text in valid_results)
        return final_text

    @classmethod
    async def _ocr_batch(cls, start_idx: int, images: List, filepath: str) -> Tuple[int, str]:
        # Convert images to base64
        content_items = [
            {"type": "text", "text": "Ты — профессиональный OCR-движок. Распознай текст с этих страниц нормативного акта ЦБ РФ. Верни только чистый распознанный текст, сохраняя структуру таблиц. Не добавляй отсебятины и комментариев."}
        ]
        
        for img in images:
            buffered = BytesIO()
            img.save(buffered, format="JPEG")
            img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
            content_items.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img_str}"}
            })
            
        messages = [{"role": "user", "content": content_items}]
        
        model = os.getenv("META_MODEL_NAME", "vertex_ai/gemini-1.5-flash")
        vertex_project = os.getenv("VERTEX_PROJECT")
        vertex_location = os.getenv("VERTEX_LOCATION", "global")
        reasoning_effort = os.getenv("META_REASONING_EFFORT", "medium")
        
        try:
            response = await acompletion(
                model=model,
                messages=messages,
                vertex_project=vertex_project,
                vertex_location=vertex_location,
                reasoning_effort=reasoning_effort
            )
            return start_idx, response.choices[0].message.content or ""
        except Exception as e:
            cls._log_error(filepath, f"LLM OCR API error: {e}", start_idx)
            return start_idx, ""

    @classmethod
    async def _extract_doc(cls, filepath: str) -> str:
        """Эвристическое извлечение текста из бинарного Word 97-2003 (OLE2)."""
        def _read():
            try:
                ole = olefile.OleFileIO(filepath)
                if not ole.exists('WordDocument'):
                    return ""
                data = ole.openstream('WordDocument').read()
                # Для русскоязычных архивов ЦБ РФ используется cp1251
                text_ansi = data.decode('cp1251', errors='ignore')
                readable = re.findall(r'[А-Яа-яA-Za-z0-9\s.,;:\-!?()]+', text_ansi)
                text_joined = "\n".join(readable)
                lines = [line.strip() for line in text_joined.split('\n') if len(line.strip()) > 10]
                return "\n".join(lines)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Failed to extract .doc {filepath}: {e}")
                return ""
        return await asyncio.to_thread(_read)

    @classmethod
    async def _extract_odt(cls, filepath: str) -> str:
        """Извлечение текста из OpenDocument Text."""
        def _read():
            try:
                from odf.opendocument import load
                from odf.text import P
                doc = load(filepath)
                text = []
                for p in doc.getElementsByType(P):
                    text.append(str(p))
                return "\n".join(text)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Failed to extract .odt {filepath}: {e}")
                return ""
        return await asyncio.to_thread(_read)
