"""Проверяет разбор XML Content AI и отказ клиента без файла, секрета или допустимого размера."""

import pytest

from src.admin_client.backend.core.utils.content_ai import ContentCaptureRecognizer, parse_result_xml

_CONTENT_AI_ENV = (
    "CONTENTAI_USERNAME",
    "CONTENTAI_PASSWORD",
    "CONTENT_AI_API_URI",
    "CONTENT_AI_PROJECT",
    "CONTENTAI_MAX_WAIT_SECONDS",
    "CONTENTAI_IDLE_TIMEOUT_SECONDS",
)


def _clear_content_ai_env(monkeypatch):
    """Убирает переменные Content AI, чтобы тест не взял значение из окружения процесса."""
    for name in _CONTENT_AI_ENV:
        monkeypatch.delenv(name, raising=False)


def _recognizer(monkeypatch) -> ContentCaptureRecognizer:
    """Собирает клиент на фиктивных реквизитах, без обращения к серверу."""
    _clear_content_ai_env(monkeypatch)
    return ContentCaptureRecognizer(
        username="ocr-user",
        password="ocr-password-not-real",
        api_uri="https://content-ai.invalid/soap",
    )


def test_parse_result_xml_reads_nested_and_repeated_fields():
    """Проверяет, что пространство имён снимается, вложенность сохраняется, повтор тега становится списком."""
    xml = (
        "<Documents xmlns='urn:https://www.contentai.ru/ContentCapture'>"
        "<Document>"
        "<Name>Договор</Name>"
        "<Field>один</Field>"
        "<Field>два</Field>"
        "<Party><Role>заёмщик</Role></Party>"
        "</Document>"
        "</Documents>"
    )
    assert parse_result_xml(xml) == {
        "Name": "Договор",
        "Field": ["один", "два"],
        "Party": {"Role": "заёмщик"},
    }


@pytest.mark.parametrize(
    "xml_text",
    [
        "это не xml",
        "<Other><Name>Договор</Name></Other>",
        "<Documents></Documents>",
        "",
    ],
)
def test_parse_result_xml_returns_empty_dict_for_unusable_input(xml_text):
    """Проверяет, что битый, чужой и пустой документ не выдаёт частичный разбор."""
    assert parse_result_xml(xml_text) == {}


def test_recognizer_accepts_explicit_credentials(monkeypatch):
    """Проверяет, что явные реквизиты открывают сессию и берут проект FullText."""
    client = _recognizer(monkeypatch)
    assert client.username == "ocr-user"
    assert client.api_uri == "https://content-ai.invalid/soap"
    assert client.project_name == "FullText"
    assert client.session.auth.username == "ocr-user"
    assert client.password == "ocr-password-not-real"


def test_recognizer_rejects_missing_credentials(monkeypatch):
    """Проверяет, что пустые имя и пароль останавливают клиент до SOAP-вызова."""
    _clear_content_ai_env(monkeypatch)
    with pytest.raises(ValueError, match="CONTENTAI_USERNAME или CONTENTAI_PASSWORD"):
        ContentCaptureRecognizer(username="", password="", api_uri="https://content-ai.invalid/soap")


def test_recognizer_rejects_masked_password(monkeypatch):
    """Проверяет, что маска из восьми звёзд не принимается как пароль."""
    _clear_content_ai_env(monkeypatch)
    monkeypatch.setenv("CONTENTAI_PASSWORD", "********")
    with pytest.raises(ValueError, match="CONTENTAI_USERNAME или CONTENTAI_PASSWORD"):
        ContentCaptureRecognizer(
            username="ocr-user",
            password="********",
            api_uri="https://content-ai.invalid/soap",
        )


def test_recognizer_rejects_missing_api_uri(monkeypatch):
    """Проверяет, что без адреса API клиент не создаётся."""
    _clear_content_ai_env(monkeypatch)
    with pytest.raises(ValueError, match="CONTENT_AI_API_URI"):
        ContentCaptureRecognizer(username="ocr-user", password="ocr-password-not-real", api_uri="")


def test_recognize_rejects_missing_file(monkeypatch, tmp_path):
    """Проверяет, что отсутствующий файл не уходит в SOAP."""
    client = _recognizer(monkeypatch)
    missing = tmp_path / "missing.pdf"
    with pytest.raises(FileNotFoundError, match="Файл не найден"):
        client.recognize(str(missing))


def test_recognize_rejects_oversized_file(monkeypatch, tmp_path):
    """Проверяет, что файл больше 100 МБ отклоняется до чтения тела в base64."""
    client = _recognizer(monkeypatch)
    source = tmp_path / "large.pdf"
    source.write_bytes(b"x")
    monkeypatch.setattr(
        "src.admin_client.backend.core.utils.content_ai.os.path.getsize",
        lambda path: 101 * 1024 * 1024,
    )
    with pytest.raises(ValueError, match="слишком большой"):
        client.recognize(str(source))
