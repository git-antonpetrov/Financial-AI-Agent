"""Тестовый выпуск корневого сертификата и клиентского сертификата."""

import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def create_test_ca_and_client_cert(
    cn: str = "superadmin-workstation",
    role: str = "admin_operator",
    client_private_key: rsa.RSAPrivateKey | None = None,
    validity_days: int = 365,
) -> tuple[str, str, rsa.RSAPrivateKey]:
    """Выпускает тестовый Root CA и подписанный им клиентский сертификат.

    Аргументы:
        cn: Common Name клиента.
        role: Значение OU клиента.
        client_private_key: Готовый ключ клиента. Если не передан, генерируется новый.
        validity_days: Срок действия клиентского сертификата в днях.

    Возвращает:
        Корневой PEM, клиентский PEM и закрытый ключ клиента.
    """
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Financial AI Agent Root Authority"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Financial AI Agent Root CA"),
        ]
    )
    now = datetime.datetime.now(datetime.UTC)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    cli_key = client_private_key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cli_name = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Financial AI Agent Workstation"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, role),
            x509.NameAttribute(NameOID.COMMON_NAME, cn),
        ]
    )
    cli_cert = (
        x509.CertificateBuilder()
        .subject_name(cli_name)
        .issuer_name(ca_name)
        .public_key(cli_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=validity_days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    cli_pem = cli_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    return ca_pem, cli_pem, cli_key
