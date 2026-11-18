"""Проверяет чтение подписанного списка отзыва и отказ шлюза по серийному номеру."""

import datetime
import json
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from starlette.requests import Request

from src.common.crl import client_serial_is_revoked, parse_serial_number
from src.simulations.api.main import rejected_revoked_client


def _identity(common_name: str) -> x509.Name:
    """Возвращает имя издателя для тестового сертификата и списка отзыва."""
    return x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )


def _write_certificate(path: Path, key, subject: x509.Name, issuer: x509.Name, issuer_key) -> None:
    """Записывает сертификат, которым проверяется подпись списка отзыва."""
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .sign(issuer_key, hashes.SHA256())
    )
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


def _write_crl(path: Path, issuer: x509.Name, issuer_key, serials: list[int]) -> None:
    """Записывает PEM-список отзыва с указанными серийными номерами."""
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(issuer)
        .last_update(now)
        .next_update(now + datetime.timedelta(days=7))
    )
    for serial in serials:
        revoked = x509.RevokedCertificateBuilder().serial_number(serial).revocation_date(now).build()
        builder = builder.add_revoked_certificate(revoked)
    signed = builder.sign(issuer_key, hashes.SHA256())
    path.write_bytes(signed.public_bytes(serialization.Encoding.PEM))


def _material(tmp_path: Path, serials: list[int], forged: bool = False) -> tuple[Path, Path]:
    """Готовит корневой сертификат и список отзыва. Поддельный список подписывает чужой ключ."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signer = rsa.generate_private_key(public_exponent=65537, key_size=2048) if forged else root_key
    name = _identity("Financial AI Root CA")
    crl_path = tmp_path / "ca.crl"
    ca_path = tmp_path / "ca.crt"
    _write_crl(crl_path, name, signer, serials)
    _write_certificate(ca_path, root_key, name, name, root_key)
    return crl_path, ca_path


def _request(serial: str | None) -> Request:
    """Собирает запрос шлюза с серийным номером клиентского сертификата."""
    headers = []
    if serial is not None:
        headers.append((b"x-client-cert-serial", serial.encode("utf-8")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "url": "http://testserver/bank/accounts",
            "headers": headers,
        }
    )


def _detail(response) -> str:
    """Возвращает текст отказа из JSON-ответа шлюза."""
    return json.loads(response.body.decode("utf-8"))["detail"]


def test_parse_serial_number_reads_decimal_hex_and_rejects_garbage():
    """Проверяет десятичную и шестнадцатеричную запись номера и отказ на пустой строке."""
    assert parse_serial_number("  ") == set()
    assert parse_serial_number("zz") == set()
    assert parse_serial_number("10") == {10, 16}
    assert parse_serial_number("0x0A") == {10}
    assert parse_serial_number("0A:0B") == {0x0A0B}


def test_client_serial_is_revoked_matches_decimal_and_caddy_hex(tmp_path: Path):
    """Проверяет, что отозванный номер находится и в десятичной записи, и в hex от Caddy."""
    crl_path, ca_path = _material(tmp_path, [10])
    assert client_serial_is_revoked("10", crl_path=str(crl_path), ca_path=str(ca_path)) is True
    assert client_serial_is_revoked("0A", crl_path=str(crl_path), ca_path=str(ca_path)) is True
    assert client_serial_is_revoked("11", crl_path=str(crl_path), ca_path=str(ca_path)) is False
    assert client_serial_is_revoked("   ", crl_path=str(crl_path), ca_path=str(ca_path)) is False

    hex_collision, ca_collision = _material(tmp_path / "collision", [16])
    assert client_serial_is_revoked("10", crl_path=str(hex_collision), ca_path=str(ca_collision)) is True


def test_client_serial_is_revoked_rejects_forged_and_unreadable_list(tmp_path: Path):
    """Проверяет отказ, если список подписан чужим ключом или не читается."""
    crl_path, ca_path = _material(tmp_path, [10], forged=True)
    with pytest.raises(ValueError, match="Подпись списка отзыва"):
        client_serial_is_revoked("10", crl_path=str(crl_path), ca_path=str(ca_path))

    broken = tmp_path / "broken.crl"
    broken.write_text("not a crl", encoding="utf-8")
    with pytest.raises(ValueError):
        client_serial_is_revoked("10", crl_path=str(broken), ca_path=str(ca_path))


def test_client_serial_is_revoked_requires_crl_and_ca_files(tmp_path: Path):
    """Проверяет, что отсутствие файла списка или корня поднимает FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        client_serial_is_revoked(
            "10",
            crl_path=str(tmp_path / "missing.crl"),
            ca_path=str(tmp_path / "missing.crt"),
        )


def test_gateway_blocks_revoked_hex_serial_and_allows_unknown(tmp_path: Path, monkeypatch):
    """Проверяет ответ 401 для отозванного hex-номера и пропуск неизвестного номера."""
    crl_path, ca_path = _material(tmp_path, [10])
    monkeypatch.setenv("CRL_PATH", str(crl_path))
    monkeypatch.setenv("ROOT_CA_CERT_PATH", str(ca_path))
    monkeypatch.setenv("STRICT_SECURITY", "true")

    revoked = rejected_revoked_client(_request("0A"))
    assert revoked is not None
    assert revoked.status_code == 401
    assert _detail(revoked) == "Клиентский сертификат отозван"
    assert rejected_revoked_client(_request("11")) is None
    assert rejected_revoked_client(_request(None)) is None


def test_gateway_fails_closed_on_forged_or_missing_crl(tmp_path: Path, monkeypatch):
    """Проверяет ответ 503, если список подделан или файл списка отсутствует."""
    crl_path, ca_path = _material(tmp_path, [10], forged=True)
    monkeypatch.setenv("CRL_PATH", str(crl_path))
    monkeypatch.setenv("ROOT_CA_CERT_PATH", str(ca_path))
    monkeypatch.setenv("STRICT_SECURITY", "true")
    forged = rejected_revoked_client(_request("10"))
    assert forged is not None
    assert forged.status_code == 503
    assert _detail(forged) == "Список отзыва сертификатов недействителен"

    monkeypatch.setenv("CRL_PATH", str(tmp_path / "missing.crl"))
    missing = rejected_revoked_client(_request("10"))
    assert missing is not None
    assert missing.status_code == 503
    assert _detail(missing) == "Список отзыва сертификатов недоступен"
