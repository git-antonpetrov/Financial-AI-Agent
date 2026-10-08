import datetime
import ipaddress
import os
from pathlib import Path
from typing import List, Optional, Tuple

from cryptography import x509
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .ca_engine import RootCAEngine


class CertificateIssuer:
    """
    Эмитент X.509 сертификатов, подписанных доверенным Root CA.
    
    Управляет генерацией пар ключей сервисов и выпуском сертификатов для:
    - Банковского шлюза (server + client auth, SAN dns/ip)
    - Сервера Администратора (server + client auth)
    - Клиента Администратора (client auth, mTLS)
    - Внутренней инфраструктуры (PostgreSQL, Redis, MinIO)
    """

    def __init__(self, ca_engine: RootCAEngine):
        self.ca_engine = ca_engine

    def issue_certificate(
        self,
        common_name: str,
        role: str = "infrastructure",
        san_dns_names: Optional[List[str]] = None,
        san_ip_addresses: Optional[List[str]] = None,
        validity_days: int = 365,
        key_size: int = 2048,
        is_server: bool = True,
        is_client: bool = False,
        is_ca: bool = False,
    ) -> Tuple[x509.Certificate, rsa.RSAPrivateKey]:
        """
        Генерирует пару ключей сервиса и выпускает X.509 сертификат, подписанный Root CA.
        """
        ca_cert = self.ca_engine.get_ca_cert()
        ca_private_key = self.ca_engine.get_ca_private_key()
        serial_number = self.ca_engine.get_next_serial()

        # 1. Генерация пары ключей сервиса
        service_private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=key_size,
            backend=default_backend(),
        )

        # 2. Формирование Subject
        subject = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Financial AI Agent Platform"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, role),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ])

        now = datetime.datetime.now(datetime.timezone.utc)

        # 3. Базовый билдер сертификата
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(ca_cert.subject)
            .public_key(service_private_key.public_key())
            .serial_number(serial_number)
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=validity_days))
            .add_extension(
                x509.BasicConstraints(ca=is_ca, path_length=0 if is_ca else None),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(service_private_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_private_key.public_key()),
                critical=False,
            )
        )

        # 4. Назначение ключа (Key Usage)
        if is_ca:
            key_usage = x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            )
        else:
            key_usage = x509.KeyUsage(
                digital_signature=True,
                content_commitment=True,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            )
        builder = builder.add_extension(key_usage, critical=True)

        # 5. Расширенное назначение ключа (Extended Key Usage)
        eku_list = []
        if is_server:
            eku_list.append(ExtendedKeyUsageOID.SERVER_AUTH)
        if is_client:
            eku_list.append(ExtendedKeyUsageOID.CLIENT_AUTH)

        if eku_list:
            builder = builder.add_extension(
                x509.ExtendedKeyUsage(eku_list),
                critical=False,
            )

        # 6. Альтернативные имена субъекта (SAN)
        san_general_names: List[x509.GeneralName] = []
        if san_dns_names:
            for dns in san_dns_names:
                if dns and dns.strip():
                    san_general_names.append(x509.DNSName(dns.strip()))

        if san_ip_addresses:
            for ip_str in san_ip_addresses:
                if ip_str and ip_str.strip():
                    try:
                        ip_obj = ipaddress.ip_address(ip_str.strip())
                        san_general_names.append(x509.IPAddress(ip_obj))
                    except ValueError:
                        pass

        if san_general_names:
            builder = builder.add_extension(
                x509.SubjectAlternativeName(san_general_names),
                critical=False,
            )

        # 7. Подписание сертификата приватным ключом Root CA
        certificate = builder.sign(
            private_key=ca_private_key,
            algorithm=hashes.SHA256(),
            backend=default_backend(),
        )

        return certificate, service_private_key

    @staticmethod
    def save_cert_and_key(
        cert: x509.Certificate,
        key: rsa.RSAPrivateKey,
        cert_path: Path | str,
        key_path: Path | str,
        key_passphrase: Optional[str] = None,
    ) -> None:
        """
        Сохраняет сертификат и приватный ключ в PEM формате.
        """
        c_path = Path(cert_path)
        k_path = Path(key_path)

        c_path.parent.mkdir(parents=True, exist_ok=True)
        k_path.parent.mkdir(parents=True, exist_ok=True)

        # Запись публичного сертификата
        c_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

        # Запись приватного ключа
        if key_passphrase:
            enc_algo = serialization.BestAvailableEncryption(key_passphrase.encode("utf-8"))
        else:
            enc_algo = serialization.NoEncryption()

        key_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=enc_algo,
        )
        k_path.write_bytes(key_pem)

        try:
            os.chmod(k_path, 0o600)
        except (AttributeError, PermissionError, OSError):
            pass
