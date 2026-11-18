"""
Модуль тестирования функционала поиска исполняемого файла конвертера LibreOffice.
"""

import importlib.util
import os
import sys

import pytest


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


def test_find_soffice_not_found(monkeypatch):
    """Проверяет, что отсутствие soffice завершается отказом, а не пустым путём."""
    mod = load_pdf_converter()
    monkeypatch.delenv("LIBREOFFICE_PATH", raising=False)
    monkeypatch.delenv("SOFFICE_PATH", raising=False)
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(mod.os.path, "isfile", lambda path: False)
    with pytest.raises(FileNotFoundError, match="LibreOffice"):
        mod.find_soffice_executable()


def test_convert_to_pdf_returns_generated_file(monkeypatch, tmp_path):
    """Проверяет, что успешный вызов LibreOffice возвращает путь к созданному PDF."""
    mod = load_pdf_converter()
    source = tmp_path / "note.txt"
    source.write_text("договор", encoding="utf-8")
    output_dir = tmp_path / "out"
    monkeypatch.setattr(mod, "find_soffice_executable", lambda: sys.executable)

    def fake_run(command, check, stdout, stderr, timeout):
        """Записывает PDF туда, куда его положил бы soffice."""
        input_path = command[5]
        target_dir = command[7]
        base = os.path.splitext(os.path.basename(input_path))[0]
        os.makedirs(target_dir, exist_ok=True)
        with open(os.path.join(target_dir, f"{base}.pdf"), "wb") as handle:
            handle.write(b"%PDF-1.4")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    result = mod.convert_to_pdf(str(source), str(output_dir))
    assert result == os.path.join(str(output_dir), "note.pdf")
    assert os.path.isfile(result)


def test_convert_to_pdf_rejects_missing_output(monkeypatch, tmp_path):
    """Проверяет, что нулевой код LibreOffice без файла PDF не считается успехом."""
    mod = load_pdf_converter()
    source = tmp_path / "note.txt"
    source.write_text("договор", encoding="utf-8")
    monkeypatch.setattr(mod, "find_soffice_executable", lambda: sys.executable)
    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: None)
    with pytest.raises(FileNotFoundError, match="PDF не был сгенерирован"):
        mod.convert_to_pdf(str(source), str(tmp_path / "out"))
