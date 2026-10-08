import datetime
import os
from pathlib import Path
from typing import Tuple, Optional

from cryptography import x509
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.backends import default_backend


class BankCertificateAuthority:
    """
    Локальный x509 Root CA и центр выпуска сертификатов для Банковского контура (Блок 5 архитектуры).
    Управляет выпуском корневого CA-сертификата и рабочего сертификата Банка.
    """

    def __init__(
        self,
        ca_cert: Optional[x509.Certificate] = None,
        ca_private_key: Optional[rsa.RSAPrivateKey] = None,
        bank_cert: Optional[x509.Certificate] = None,
        bank_private_key: Optional[rsa.RSAPrivateKey] = None,
    ):
        self.ca_cert = ca_cert
        self.ca_private_key = ca_private_key
        self.bank_cert = bank_cert
        self.bank_private_key = bank_private_key

    @classmethod
    def create_in_memory(cls, key_size: int = 2048) -> "BankCertificateAuthority":
        """
        Создает новый экземпляр Root CA и выпускает сертификат Банка в оперативной памяти.
        """
        instance = cls()
        instance.initialize_ca(key_size=key_size)
        instance.issue_bank_certificate(key_size=key_size)
        return instance

    def initialize_ca(
        self,
        key_size: int = 2048,
        common_name: str = "Financial AI Agent Root CA",
        validity_days: int = 3650,
    ) -> Tuple[x509.Certificate, rsa.RSAPrivateKey]:
        """
        Генерирует новую пару ключей Root CA и самоподписанный X.509 сертификат.
        """
        # 1. Генерация ключа CA
        ca_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=key_size,
            backend=default_backend(),
        )

        # 2. Формирование субъекта и издателя
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Financial AI Agent Core"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Root Security Authority"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ])

        now = datetime.datetime.now(datetime.timezone.utc)
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=validity_days))
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=1),
                critical=True,
            )
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
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256(), default_backend())
        )

        self.ca_cert = ca_cert
        self.ca_private_key = ca_key
        return ca_cert, ca_key

    def issue_bank_certificate(
        self,
        key_size: int = 2048,
        common_name: str = "Financial AI Simulation Bank",
        validity_days: int = 365,
    ) -> Tuple[x509.Certificate, rsa.RSAPrivateKey]:
        """
        Выпускает сертификат Банка, подписанный Root CA.
        """
        if not self.ca_cert or not self.ca_private_key:
            raise RuntimeError("Root CA не инициализирован. Вызовите initialize_ca() перед выпуском сертификата банка.")

        bank_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=key_size,
            backend=default_backend(),
        )

        subject = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Financial AI Agent Core"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Simulation Bank Domain"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ])

        now = datetime.datetime.now(datetime.timezone.utc)
        bank_cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(self.ca_cert.subject)
            .public_key(bank_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=validity_days))
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None),
                critical=True,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=True,
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
                x509.ExtendedKeyUsage([
                    ExtendedKeyUsageOID.SERVER_AUTH,
                    ExtendedKeyUsageOID.CLIENT_AUTH,
                ]),
                critical=False,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(bank_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self.ca_private_key.public_key()),
                critical=False,
            )
            .sign(self.ca_private_key, hashes.SHA256(), default_backend())
        )

        self.bank_cert = bank_cert
        self.bank_private_key = bank_key
        return bank_cert, bank_key

    # --- Сериализация и экспорт в PEM ---

    def get_ca_cert_pem(self) -> str:
        if not self.ca_cert:
            raise RuntimeError("CA сертификат отсутствует")
        return self.ca_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")

    def get_bank_cert_pem(self) -> str:
        if not self.bank_cert:
            raise RuntimeError("Сертификат банка отсутствует")
        return self.bank_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")

    def get_bank_private_key_pem(self) -> str:
        if not self.bank_private_key:
            raise RuntimeError("Приватный ключ банка отсутствует")
        return self.bank_private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")

    def get_bank_public_key_pem(self) -> str:
        if not self.bank_cert:
            raise RuntimeError("Сертификат банка отсутствует")
        return self.bank_cert.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")

    @staticmethod
    def get_cert_fingerprint(cert: x509.Certificate) -> str:
        """Возвращает SHA-256 отпечаток сертификата в шестнадцатеричном виде."""
        return cert.fingerprint(hashes.SHA256()).hex()

    def get_bank_cert_fingerprint(self) -> str:
        if not self.bank_cert:
            raise RuntimeError("Сертификат банка отсутствует")
        return self.get_cert_fingerprint(self.bank_cert)

    # --- Валидация сертификата ---

    @classmethod
    def verify_certificate_against_ca(cls, cert_pem: str, ca_cert_pem: str) -> bool:
        """
        Проверяет, что cert_pem подписан ca_cert_pem и действителен по времени.
        """
        try:
            cert = x509.load_pem_x509_certificate(cert_pem.encode("utf-8"), default_backend())
            ca = x509.load_pem_x509_certificate(ca_cert_pem.encode("utf-8"), default_backend())

            # 1. Проверка срока действия
            now = datetime.datetime.now(datetime.timezone.utc)
            if now < cert.not_valid_before_utc or now > cert.not_valid_after_utc:
                return False

            # 2. Проверка соответствия издателя
            if cert.issuer != ca.subject:
                return False

            # 3. Криптографическая проверка подписи открытым ключом CA
            ca_public_key = ca.public_key()
            if isinstance(ca_public_key, rsa.RSAPublicKey):
                ca_public_key.verify(
                    cert.signature,
                    cert.tbs_certificate_bytes,
                    padding.PKCS1v15(),
                    cert.signature_hash_algorithm,
                )
                return True
            return False
        except Exception:
            return False

    # --- Сохранение и загрузка на диск ---

    def save_to_dir(self, directory_path: str) -> None:
        """Сохраняет сертификаты и ключи в указанную директорию."""
        path = Path(directory_path)
        path.mkdir(parents=True, exist_ok=True)

        if self.ca_cert:
            (path / "ca.crt").write_text(self.get_ca_cert_pem(), encoding="utf-8")
        if self.ca_private_key:
            ca_key_pem = self.ca_private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ).decode("utf-8")
            (path / "ca.key").write_text(ca_key_pem, encoding="utf-8")
        if self.bank_cert:
            (path / "bank.crt").write_text(self.get_bank_cert_pem(), encoding="utf-8")
        if self.bank_private_key:
            (path / "bank.key").write_text(self.get_bank_private_key_pem(), encoding="utf-8")

    @classmethod
    def load_from_dir(cls, directory_path: str) -> Optional["BankCertificateAuthority"]:
        """Пытается загрузить сертификаты и ключи из указанной директории."""
        path = Path(directory_path)
        ca_crt_file = path / "ca.crt"
        bank_crt_file = path / "bank.crt"
        bank_key_file = path / "bank.key"

        if not (ca_crt_file.exists() and bank_crt_file.exists() and bank_key_file.exists()):
            return None

        try:
            ca_cert = x509.load_pem_x509_certificate(ca_crt_file.read_bytes(), default_backend())
            bank_cert = x509.load_pem_x509_certificate(bank_crt_file.read_bytes(), default_backend())
            bank_key = serialization.load_pem_private_key(
                bank_key_file.read_bytes(),
                password=None,
                backend=default_backend(),
            )
            ca_key = None
            ca_key_file = path / "ca.key"
            if ca_key_file.exists():
                ca_key = serialization.load_pem_private_key(
                    ca_key_file.read_bytes(),
                    password=None,
                    backend=default_backend(),
                )

            instance = cls(
                ca_cert=ca_cert,
                ca_private_key=ca_key,
                bank_cert=bank_cert,
                bank_private_key=bank_key,
            )
            return instance
        except Exception:
            return None


    @classmethod
    def load_from_central_pki(
        cls,
        ca_cert_path: Optional[str] = None,
        bank_cert_path: Optional[str] = None,
        bank_key_path: Optional[str] = None,
    ) -> Optional["BankCertificateAuthority"]:
        """
        Загружает доверенный сертификат Root CA (/certs/ca.crt), 
        сертификат банка (/certs/bank.crt) и приватный ключ банка (/certs/bank.key),
        выпущенные центральным Root CA.
        """
        ca_candidates = [
            ca_cert_path or "",
            os.getenv("ROOT_CA_CERT_PATH", ""),
            "/certs/ca.crt",
            "./certs/ca.crt",
            str(Path(__file__).resolve().parents[4] / "certs" / "ca.crt"),
        ]
        bank_cert_candidates = [
            bank_cert_path or "",
            os.getenv("BANK_CERT_PATH", ""),
            "/certs/bank.crt",
            "./certs/bank.crt",
            str(Path(__file__).resolve().parents[4] / "certs" / "bank.crt"),
        ]
        bank_key_candidates = [
            bank_key_path or "",
            os.getenv("BANK_KEY_PATH", ""),
            "/certs/bank.key",
            "./certs/bank.key",
            str(Path(__file__).resolve().parents[4] / "certs" / "bank.key"),
        ]

        ca_file = next((Path(p) for p in ca_candidates if p and Path(p).exists()), None)
        bank_cert_file = next((Path(p) for p in bank_cert_candidates if p and Path(p).exists()), None)
        bank_key_file = next((Path(p) for p in bank_key_candidates if p and Path(p).exists()), None)

        if not (ca_file and bank_cert_file and bank_key_file):
            return None

        try:
            ca_cert = x509.load_pem_x509_certificate(ca_file.read_bytes(), default_backend())
            bank_cert = x509.load_pem_x509_certificate(bank_cert_file.read_bytes(), default_backend())
            bank_key = serialization.load_pem_private_key(
                bank_key_file.read_bytes(),
                password=None,
                backend=default_backend(),
            )

            # Валидируем сертификат банка по доверенному Root CA
            if not cls.verify_certificate_against_ca(
                bank_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8"),
                ca_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
            ):
                return None

            return cls(
                ca_cert=ca_cert,
                ca_private_key=None,  # Приватный ключ Root CA изолирован в микросервисе Root CA!
                bank_cert=bank_cert,
                bank_private_key=bank_key,
            )
        except Exception:
            return None


# Глобальный Singleton-экземпляр Bank CA
_BANK_CA_INSTANCE: Optional[BankCertificateAuthority] = None


def get_bank_ca() -> BankCertificateAuthority:
    """
    Возвращает синглтон-экземпляр Bank CA.
    Приоритет:
    1. Централизованный Zero-Trust Root CA (/certs/ca.crt + bank.crt + bank.key);
    2. Локальная директория сертификатов BANK_CERTS_DIR;
    3. Автоматическая генерация в памяти (dev/test fallback).
    """
    global _BANK_CA_INSTANCE
    if _BANK_CA_INSTANCE is not None:
        return _BANK_CA_INSTANCE

    # 1. Попытка загрузки из центрального контура PKI
    instance = BankCertificateAuthority.load_from_central_pki()

    # 2. Попытка загрузки из локальной директории
    if instance is None:
        certs_dir = os.getenv("BANK_CERTS_DIR")
        if not certs_dir:
            certs_dir = str(Path(__file__).resolve().parent / "certs")
        instance = BankCertificateAuthority.load_from_dir(certs_dir)

    # 3. Эфемерная генерация в памяти
    if instance is None:
        instance = BankCertificateAuthority.create_in_memory()
        try:
            certs_dir = os.getenv("BANK_CERTS_DIR") or str(Path(__file__).resolve().parent / "certs")
            instance.save_to_dir(certs_dir)
        except Exception:
            pass

    _BANK_CA_INSTANCE = instance
    return _BANK_CA_INSTANCE


def reset_bank_ca_for_tests() -> BankCertificateAuthority:
    """Сбрасывает синглтон (для изолированного тестирования)."""
    global _BANK_CA_INSTANCE
    _BANK_CA_INSTANCE = BankCertificateAuthority.create_in_memory()
    return _BANK_CA_INSTANCE
