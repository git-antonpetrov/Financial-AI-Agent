#!/usr/bin/env python3
"""
Кроссплатформенный генератор внутренних сертификатов PKI (Root CA, Caddy TLS, Client mTLS,
PostgreSQL TLS, Redis TLS, MinIO TLS) с использованием библиотеки cryptography.
Работает одинаково на Linux, Windows и macOS без внешних бинарников openssl.
"""

import os
import sys
import datetime
from pathlib import Path
import ipaddress

from cryptography import x509
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12


def create_rsa_key(key_size: int = 2048) -> rsa.RSAPrivateKey:
    """Генерирует приватный RSA ключ."""
    return rsa.generate_private_key(
        public_exponent=65537,
        key_size=key_size,
    )


def save_key(key: rsa.RSAPrivateKey, path: Path, mode: int = 0o600):
    """Сохраняет приватный ключ в PEM-формате."""
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(pem)
    try:
        os.chmod(path, mode)
    except Exception:
        pass


def save_cert(cert: x509.Certificate, path: Path, mode: int = 0o644):
    """Сохраняет X.509 сертификат в PEM-формате."""
    pem = cert.public_bytes(serialization.Encoding.PEM)
    path.write_bytes(pem)
    try:
        os.chmod(path, mode)
    except Exception:
        pass


def generate_all_certs(certs_dir: Path | None = None) -> dict[str, str]:
    """
    Генерирует полный набор сертификатов для Zero-Trust среды:
    - Root CA (ca.crt, ca.key)
    - Caddy / Edge Server (server.crt, server.key)
    - Desktop / Admin Client mTLS (client.crt, client.key, client.p12)
    - PostgreSQL (postgres.crt, postgres.key)
    - Redis (redis.crt, redis.key)
    - MinIO (minio.crt, minio.key, certs/minio/public.crt, certs/minio/private.key)
    """
    if certs_dir is None:
        certs_dir = Path(__file__).resolve().parent.parent / "certs"
    certs_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_expiry = now + datetime.timedelta(days=3650)  # 10 лет
    cert_expiry = now + datetime.timedelta(days=825)  # ~2 года

    # 1. Root CA
    ca_key_path = certs_dir / "ca.key"
    ca_crt_path = certs_dir / "ca.crt"
    if ca_key_path.exists() and ca_crt_path.exists():
        ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
        ca_crt = x509.load_pem_x509_certificate(ca_crt_path.read_bytes())
    else:
        ca_key = create_rsa_key(4096)
        ca_name = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "FinancialAI"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Security"),
            x509.NameAttribute(NameOID.COMMON_NAME, "FinancialAI Internal Root CA"),
        ])
        ca_crt = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(ca_expiry)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(ca_key, hashes.SHA256())
        )
        save_key(ca_key, ca_key_path)
        save_cert(ca_crt, ca_crt_path)

    def issue_cert(
        common_name: str,
        san_dns: list[str],
        san_ips: list[str],
        extended_usages: list[ExtendedKeyUsageOID],
        key_filename: str,
        cert_filename: str,
    ) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
        key_path = certs_dir / key_filename
        cert_path = certs_dir / cert_filename

        # Если сертификат и ключ уже существуют и действительны еще минимум 30 дней, повторно используем их
        if key_path.exists() and cert_path.exists():
            try:
                existing_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
                existing_cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
                cert_valid_until = getattr(existing_cert, "not_valid_after_utc", None) or existing_cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)
                if cert_valid_until > now + datetime.timedelta(days=30):
                    return existing_key, existing_cert
            except Exception:
                pass

        key = create_rsa_key(2048)
        subject_name = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "FinancialAI"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ])

        general_names: list[x509.GeneralName] = [x509.DNSName(dns) for dns in san_dns]
        for ip in san_ips:
            general_names.append(x509.IPAddress(ipaddress.ip_address(ip)))

        builder = (
            x509.CertificateBuilder()
            .subject_name(subject_name)
            .issuer_name(ca_crt.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(cert_expiry)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=True,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage(extended_usages),
                critical=False,
            )
        )
        if general_names:
            builder = builder.add_extension(
                x509.SubjectAlternativeName(general_names),
                critical=False,
            )

        cert = builder.sign(ca_key, hashes.SHA256())
        save_key(key, certs_dir / key_filename)
        save_cert(cert, certs_dir / cert_filename)
        return key, cert

    # 2. Caddy edge server
    issue_cert(
        common_name="admin.fin-ai-agent.ru",
        san_dns=["admin.fin-ai-agent.ru", "localhost", "admin-server"],
        san_ips=["127.0.0.1"],
        extended_usages=[ExtendedKeyUsageOID.SERVER_AUTH],
        key_filename="server.key",
        cert_filename="server.crt",
    )

    # 3. Client mTLS
    client_key, client_crt = issue_cert(
        common_name="admin-client",
        san_dns=["admin-client"],
        san_ips=[],
        extended_usages=[ExtendedKeyUsageOID.CLIENT_AUTH],
        key_filename="client.key",
        cert_filename="client.crt",
    )
    p12_path = certs_dir / "client.p12"
    if not p12_path.exists() or (certs_dir / "client.key").stat().st_mtime > p12_path.stat().st_mtime:
        p12_bytes = pkcs12.serialize_key_and_certificates(
            name=b"admin-client",
            key=client_key,
            cert=client_crt,
            cas=[ca_crt],
            encryption_algorithm=serialization.BestAvailableEncryption(b"financial-agent-mtls"),
        )
        p12_path.write_bytes(p12_bytes)
        try:
            os.chmod(p12_path, 0o600)
        except Exception:
            pass

    # 4. PostgreSQL
    issue_cert(
        common_name="postgres-db",
        san_dns=["postgres-db", "financial-postgres", "localhost"],
        san_ips=["127.0.0.1"],
        extended_usages=[ExtendedKeyUsageOID.SERVER_AUTH],
        key_filename="postgres.key",
        cert_filename="postgres.crt",
    )

    # 5. Redis
    issue_cert(
        common_name="redis",
        san_dns=["redis", "redis-server", "localhost"],
        san_ips=["127.0.0.1"],
        extended_usages=[ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH],
        key_filename="redis.key",
        cert_filename="redis.crt",
    )

    # 6. MinIO
    minio_key, minio_crt = issue_cert(
        common_name="minio",
        san_dns=["minio", "minio-server", "localhost"],
        san_ips=["127.0.0.1"],
        extended_usages=[ExtendedKeyUsageOID.SERVER_AUTH],
        key_filename="minio.key",
        cert_filename="minio.crt",
    )
    # Создаем структуру каталога для MinIO (/root/.minio/certs)
    minio_certs_dir = certs_dir / "minio"
    minio_certs_dir.mkdir(parents=True, exist_ok=True)
    save_key(minio_key, minio_certs_dir / "private.key")
    save_cert(minio_crt, minio_certs_dir / "public.crt")
    minio_cas_dir = minio_certs_dir / "CAs"
    minio_cas_dir.mkdir(parents=True, exist_ok=True)
    save_cert(ca_crt, minio_cas_dir / "ca.crt")

    return {
        "certs_dir": str(certs_dir),
        "ca_crt": str(ca_crt_path),
        "ca_fingerprint": ca_crt.fingerprint(hashes.SHA256()).hex().upper(),
    }


if __name__ == "__main__":
    res = generate_all_certs()
    print(f"[+] All Zero-Trust certificates successfully generated in: {res['certs_dir']}")
    print(f"[+] Root CA Fingerprint (SHA256): {res['ca_fingerprint']}")
