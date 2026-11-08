"""Читает подписанный список отзыва X.509 и сверяет серийный номер клиента."""

import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.backends import default_backend


def parse_serial_number(serial: str) -> set[int]:
    """Возвращает целые значения серийного номера в десятичной и шестнадцатеричной записи.

    Caddy передаёт серийный номер в hex, а тесты и журнал выпуска — в десятичной форме.
    Строка только из цифр поэтому даёт оба значения. Совпадение любого из них с отозванным
    номером закрывает доступ.

    Аргументы:
        serial: Серийный номер из заголовка или журнала.

    Возвращает:
        Множество целых значений. Пустое множество означает, что строка не является номером.
    """
    text = serial.strip().lower().replace(":", "")
    if text.startswith("0x"):
        text = text[2:]
    if not text:
        return set()
    values: set[int] = set()
    try:
        values.add(int(text, 16))
    except ValueError:
        return set()
    if all(character in "0123456789" for character in text):
        values.add(int(text, 10))
    return values


def client_serial_is_revoked(
    serial: str,
    crl_path: str | None = None,
    ca_path: str | None = None,
) -> bool:
    """Проверяет, есть ли серийный номер в подписанном списке отзыва.

    Аргументы:
        serial: Серийный номер клиентского сертификата.
        crl_path: Путь к ``ca.crl``. Пустое значение берётся из ``CRL_PATH`` или ``/certs/ca.crl``.
        ca_path: Путь к корневому сертификату. Пустое значение берётся из ``ROOT_CA_CERT_PATH`` или ``/certs/ca.crt``.

    Возвращает:
        True, если номер входит в список отзыва.

    Вызывает:
        FileNotFoundError: Файл списка или корневого сертификата отсутствует.
        ValueError: Список не читается или подписан другим ключом.
    """
    crl_file = Path(crl_path or os.getenv("CRL_PATH", "/certs/ca.crl"))
    ca_file = Path(ca_path or os.getenv("ROOT_CA_CERT_PATH", "/certs/ca.crt"))
    if not crl_file.is_file() or not ca_file.is_file():
        raise FileNotFoundError(crl_file)
    revocation_list = x509.load_pem_x509_crl(crl_file.read_bytes(), default_backend())
    certificate = x509.load_pem_x509_certificate(ca_file.read_bytes(), default_backend())
    if not revocation_list.is_signature_valid(certificate.public_key()):
        raise ValueError("Подпись списка отзыва не совпадает с корневым сертификатом")
    presented = parse_serial_number(serial)
    if not presented:
        return False
    return any(
        revocation_list.get_revoked_certificate_by_serial_number(number) is not None
        for number in presented
    )
