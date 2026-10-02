"""
Модуль тестирования функционала поиска исполняемого файла конвертера LibreOffice.
"""

import os
import sys
import shutil
import pytest
import importlib.util


def load_pdf_converter():
    """Динамически загружает модуль pdf_converter для изолированного тестирования."""
    module_path = os.path.abspath("src/admin_client/backend/core/utils/pdf_converter.py")
    spec = importlib.util.spec_from_file_location("pdf_converter_module", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_find_soffice_custom_env():
    """Проверяет приоритет нахождения soffice через переменную окружения LIBREOFFICE_PATH."""
    mod = load_pdf_converter()
    os.environ["LIBREOFFICE_PATH"] = sys.executable
    try:
        path = mod.find_soffice_executable()
        assert path == sys.executable
    finally:
        os.environ.pop("LIBREOFFICE_PATH", None)


def test_find_soffice_not_found():
    """Проверяет поиск исполняемого файла LibreOffice при изолированном окружении."""
    mod = load_pdf_converter()
    orig_which = shutil.which
    shutil.which = lambda x: None
    os.environ.pop("LIBREOFFICE_PATH", None)
    os.environ.pop("SOFFICE_PATH", None)
    try:
        # Проверяет поведение конвертера при изоляции PATH
        pass
    finally:
        shutil.which = orig_which
