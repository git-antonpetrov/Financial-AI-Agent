import datetime
import os
from pathlib import Path
from typing import Tuple, Optional

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .passphrase_vault import resolve_root_ca_passphrase


class RootCAEngine:
    """
    Ядро Корневого Удостоверяющего Центра (Root CA Engine).
    
    Управляет жизненным циклом 4096-битной пары ключей Root CA и корневого сертификата:
    1. Приватный ключ всегда сохраняется на диске в зашифрованном виде (PKCS#8 + AES-256).
    2. Серийные номера сертификатов инкрементируются через счетчик serial.txt.
    3. Корневой сертификат выпускается со сроком 10 лет и флагом CA=True.
    """

    DEFAULT_COMMON_NAME = "Financial AI Agent Root Authority"
    DEFAULT_ORG = "Financial AI Agent Core System"
    DEFAULT_OU = "Zero-Trust PKI Security Architecture"

    def __init__(
        self,
        data_dir: Path | str,
        passphrase: Optional[str] = None,
        key_size: int = 4096,
        validity_years: int = 10,
    ):
        self.data_dir = Path(data_dir)
        self.passphrase = passphrase or resolve_root_ca_passphrase()
        self.key_size = key_size
        self.validity_years = validity_years

        self.key_path = self.data_dir / "ca.key"
        self.cert_path = self.data_dir / "ca.crt"
        self.serial_path = self.data_dir / "serial.txt"

        self._cached_cert: Optional[x509.Certificate] = None
        self._cached_private_key: Optional[rsa.RSAPrivateKey] = None

    def ensure_initialized(self) -> Tuple[x509.Certificate, rsa.RSAPrivateKey]:
        """
        Гарантирует инициализацию Root CA: загружает существующий или генерирует новый.
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.key_path.exists() and self.cert_path.exists():
            return self.load_ca()
        return self.initialize()

    def initialize(
        self,
        common_name: Optional[str] = None,
        force: bool = False,
    ) -> Tuple[x509.Certificate, rsa.RSAPrivateKey]:
        """
        Генерирует новую пару ключей Root CA, сохраняет зашифрованный приватный ключ
        и выпускает самоподписанный корневой сертификат.
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.key_path.exists() and not force:
            raise FileExistsError(
                f"Приватный ключ Root CA уже существует по пути {self.key_path}. "
                f"Используйте force=True для принудительной перезаписи (ОПАСНО)."
            )

        cn = common_name or self.DEFAULT_COMMON_NAME

        # 1. Генерация RSA-4096 пары ключей
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=self.key_size,
            backend=default_backend(),
        )

        # 2. Сохранение приватного ключа в зашифрованном виде (PKCS#8 + AES-256)
        encrypted_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.BestAvailableEncryption(
                self.passphrase.encode("utf-8")
            ),
        )
        self.key_path.write_bytes(encrypted_pem)
        try:
            # Устанавливаем строгие права доступа (только владелец)
            os.chmod(self.key_path, 0o600)
        except (AttributeError, PermissionError, OSError):
            pass

        # 3. Формирование Subject и Issuer
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, self.DEFAULT_ORG),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, self.DEFAULT_OU),
            x509.NameAttribute(NameOID.COMMON_NAME, cn),
        ])

        now = datetime.datetime.now(datetime.timezone.utc)
        validity_days = self.validity_years * 365 + 2  # учет високосных лет

        # 4. Формирование самоподписанного сертификата X.509 Root CA
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(private_key.public_key())
            .serial_number(1)
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
                x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()),
                critical=False,
            )
        )

        cert = builder.sign(
            private_key=private_key,
            algorithm=hashes.SHA256(),
            backend=default_backend(),
        )

        self.cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        # Инициализация счетчика серийных номеров со значения 2
        self.serial_path.write_text("2\n", encoding="utf-8")

        self._cached_cert = cert
        self._cached_private_key = private_key
        return cert, private_key

    def load_ca(self) -> Tuple[x509.Certificate, rsa.RSAPrivateKey]:
        """
        Загружает сертификат и расшифровывает приватный ключ Root CA в оперативную память.
        """
        if not self.key_path.exists() or not self.cert_path.exists():
            raise FileNotFoundError(
                f"Файлы Root CA не найдены в {self.data_dir}. Выполните initialize()."
            )

        cert_bytes = self.cert_path.read_bytes()
        cert = x509.load_pem_x509_certificate(cert_bytes, default_backend())

        key_bytes = self.key_path.read_bytes()
        private_key = serialization.load_pem_private_key(
            data=key_bytes,
            password=self.passphrase.encode("utf-8"),
            backend=default_backend(),
        )

        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise TypeError("Загруженный приватный ключ Root CA не является RSA ключом.")

        self._cached_cert = cert
        self._cached_private_key = private_key
        return cert, private_key

    def get_ca_cert(self) -> x509.Certificate:
        """Возвращает публичный сертификат Root CA."""
        if self._cached_cert is not None:
            return self._cached_cert
        cert, _ = self.load_ca()
        return cert

    def get_ca_private_key(self) -> rsa.RSAPrivateKey:
        """Возвращает расшифрованный приватный ключ Root CA."""
        if self._cached_private_key is not None:
            return self._cached_private_key
        _, priv = self.load_ca()
        return priv

    def get_next_serial(self) -> int:
        """
        Атомарно считывает и инкрементирует серийный номер в serial.txt.
        """
        if not self.serial_path.exists():
            self.serial_path.write_text("2\n", encoding="utf-8")

        content = self.serial_path.read_text(encoding="utf-8").strip()
        try:
            current_serial = int(content)
        except ValueError:
            current_serial = 2

        next_serial = current_serial + 1
        # Атомарная запись через временный файл
        tmp_path = self.data_dir / "serial.txt.tmp"
        tmp_path.write_text(f"{next_serial}\n", encoding="utf-8")
        tmp_path.replace(self.serial_path)

        return current_serial

    def export_ca_certificate(self, target_path: Path | str) -> None:
        """
        Экспортирует публичный сертификат ca.crt в указанную общую директорию.
        """
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        cert = self.get_ca_cert()
        target.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
