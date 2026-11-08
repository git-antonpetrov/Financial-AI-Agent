import datetime
import json
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

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
        passphrase: str | None = None,
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
        self.revoked_path = self.data_dir / "revoked.json"

        self._cached_cert: x509.Certificate | None = None
        self._cached_private_key: rsa.RSAPrivateKey | None = None

    def ensure_initialized(self) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
        """
        Гарантирует инициализацию Root CA: загружает существующий или генерирует новый.
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.key_path.exists() and self.cert_path.exists():
            return self.load_ca()
        return self.initialize()

    def initialize(
        self,
        common_name: str | None = None,
        force: bool = False,
    ) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
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
            encryption_algorithm=serialization.BestAvailableEncryption(self.passphrase.encode("utf-8")),
        )
        self.key_path.write_bytes(encrypted_pem)
        try:
            # Устанавливает строгие права доступа (только владелец)
            os.chmod(self.key_path, 0o600)
        except (AttributeError, PermissionError, OSError):
            pass

        # 3. Формирование Subject и Issuer
        subject = issuer = x509.Name(
            [
                x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, self.DEFAULT_ORG),
                x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, self.DEFAULT_OU),
                x509.NameAttribute(NameOID.COMMON_NAME, cn),
            ]
        )

        now = datetime.datetime.now(datetime.UTC)
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

    def load_ca(self) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
        """
        Загружает сертификат и расшифровывает приватный ключ Root CA в оперативную память.
        """
        if not self.key_path.exists() or not self.cert_path.exists():
            raise FileNotFoundError(f"Файлы Root CA не найдены в {self.data_dir}. Выполните initialize().")

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

    def load_revoked_serials(self) -> list[tuple[int, datetime.datetime]]:
        """Возвращает серийные номера, записанные в реестр отзыва корневого центра.

        Возвращает:
            Пары из серийного номера и времени отзыва в UTC. Пустой реестр даёт пустой список.

        Вызывает:
            ValueError: Файл реестра повреждён и не содержит список записей.
        """
        if not self.revoked_path.is_file():
            return []
        payload = json.loads(self.revoked_path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("Реестр отзыва должен быть списком")
        revoked: list[tuple[int, datetime.datetime]] = []
        for item in payload:
            revoked_at = datetime.datetime.fromisoformat(str(item["revoked_at"]))
            if revoked_at.tzinfo is None:
                revoked_at = revoked_at.replace(tzinfo=datetime.UTC)
            revoked.append((int(item["serial"]), revoked_at))
        return revoked

    def revoke_serial(self, serial: int, revoked_at: datetime.datetime | None = None) -> None:
        """Добавляет серийный номер в реестр отзыва, если его там ещё нет.

        Аргументы:
            serial: Серийный номер выпускаемого или заменяемого сертификата.
            revoked_at: Время отзыва в UTC. Пустое значение означает текущий момент.

        Побочные эффекты:
            Создаёт или заменяет ``revoked.json`` атомарной записью.
        """
        current = self.load_revoked_serials()
        if any(existing == serial for existing, _revoked_at in current):
            return
        moment = revoked_at or datetime.datetime.now(datetime.UTC)
        current.append((serial, moment))
        payload = [
            {
                "serial": item_serial,
                "revoked_at": item_time.astimezone(datetime.UTC).isoformat(),
            }
            for item_serial, item_time in current
        ]
        temporary = self.revoked_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(self.revoked_path)

    def write_certificate_revocation_list(
        self,
        target_path: Path | str,
        revoked: list[tuple[int, datetime.datetime]] | None = None,
    ) -> None:
        """Подписывает список отзыва RFC 5280 и записывает его в PEM.

        Пустой список тоже является действующим CRL: он сообщает, что
        отозванных серийных номеров нет, и имеет срок следующей публикации
        семь суток. Пустой аргумент ``revoked`` берёт номера из реестра центра.

        Аргументы:
            target_path: Путь файла ``ca.crl``.
            revoked: Пары из серийного номера и времени отзыва в UTC.
                ``None`` читает ``revoked.json``.

        Побочные эффекты:
            Создаёт или заменяет файл списка отзыва.
        """
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if revoked is None:
            revoked = self.load_revoked_serials()
        certificate = self.get_ca_cert()
        private_key = self.get_ca_private_key()
        now = datetime.datetime.now(datetime.UTC)
        builder = (
            x509.CertificateRevocationListBuilder()
            .issuer_name(certificate.subject)
            .last_update(now)
            .next_update(now + datetime.timedelta(days=7))
        )
        for serial_number, revoked_at in revoked or []:
            revoked_certificate = (
                x509.RevokedCertificateBuilder()
                .serial_number(serial_number)
                .revocation_date(revoked_at)
                .build()
            )
            builder = builder.add_revoked_certificate(revoked_certificate)
        revocation_list = builder.sign(private_key, hashes.SHA256())
        target.write_bytes(revocation_list.public_bytes(serialization.Encoding.PEM))

    def export_ca_certificate(self, target_path: Path | str) -> None:
        """
        Экспортирует публичный сертификат ca.crt в указанную общую директорию.
        """
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        cert = self.get_ca_cert()
        target.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
