"""
Программный криптографический мост (Software Signing Bridge).
Используется для работы с файловыми ключами в изолированных стендах и тестировании.
Строго требует наличия легитимного сертификата, подписанного Root CA. Генерация самоподписанных сертификатов исключена.
"""

import os
import sys
import base64
import hashlib
from typing import Optional
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography import x509

try:
    from .cert_validator import (
        resolve_and_validate_client_certificate,
        find_certificate_file,
        CertificateMissingError,
        CertificateValidationError,
        KeyMissingError,
    )
except ImportError:
    from cert_validator import (
        resolve_and_validate_client_certificate,
        find_certificate_file,
        CertificateMissingError,
        CertificateValidationError,
        KeyMissingError,
    )


class SoftwareSigningBridge:
    """
    Программное управление ключами и сертификатами для клиентской стороны.
    Загружает приватный ключ и сертификат с диска или параметров.
    Строго требует наличия ключей и сертификата, подписанного Root CA.
    """

    def __init__(
        self,
        cert_path: Optional[str] = None,
        key_path: Optional[str] = None,
        key_pem: Optional[str] = None,
        cert_pem: Optional[str] = None,
        ca_cert_path: Optional[str] = None,
        ca_cert_pem: Optional[str] = None,
        key_password: Optional[str] = None,
    ):
        self.cert_path = cert_path
        self.key_path = key_path
        self.ca_cert_path = ca_cert_path
        self.key_password = key_password

        # 1. Разрешение приватного ключа
        self._private_key = self._resolve_private_key(key_path=key_path, key_pem=key_pem, password=key_password)

        # 2. Разрешение и строгая валидация сертификата (без самоподписанных фоллбэков)
        self._cert_pem, self._cert_obj = resolve_and_validate_client_certificate(
            cert_path=cert_path,
            cert_pem=cert_pem,
            ca_cert_path=ca_cert_path,
            ca_cert_pem=ca_cert_pem,
            expected_public_key=self._private_key.public_key()
        )

    def _resolve_private_key(
        self,
        key_path: Optional[str],
        key_pem: Optional[str],
        password: Optional[str]
    ) -> rsa.RSAPrivateKey:
        """Загружает закрытый ключ из строки PEM или файла. При отсутствии выбрасывает KeyMissingError."""
        pwd = password.encode("utf-8") if password else None

        if key_pem and key_pem.strip():
            try:
                loaded = serialization.load_pem_private_key(key_pem.strip().encode("utf-8"), password=pwd)
                if not isinstance(loaded, rsa.RSAPrivateKey):
                    raise TypeError("Поддерживаются только RSA приватные ключи")
                return loaded
            except Exception as e:
                raise KeyMissingError(f"Ошибка загрузки переданного приватного ключа: {e}")

        # Поиск файла ключа
        found_key_file = find_certificate_file(
            custom_path=key_path,
            env_var="ADMIN_CLIENT_KEY_PATH",
            default_filenames=("admin_client.key",)
        )
        if not found_key_file:
            raise KeyMissingError(
                "Приватный ключ администратора не найден. "
                "Для боевого запуска ключ должен быть установлен по пути certs/admin_client.key "
                "или передан через ADMIN_CLIENT_KEY_PATH / key_path. "
                "Генерация случайных ключей без параметров строго запрещена."
            )

        try:
            with open(found_key_file, "rb") as f:
                loaded = serialization.load_pem_private_key(f.read(), password=pwd)
                if not isinstance(loaded, rsa.RSAPrivateKey):
                    raise TypeError("Поддерживаются только RSA приватные ключи")
                return loaded
        except Exception as e:
            raise KeyMissingError(f"Ошибка чтения файла приватного ключа {found_key_file}: {e}")

    def get_public_key_pem(self) -> str:
        """Возвращает открытый ключ в формате PEM."""
        return self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode("utf-8")

    def get_key_fingerprint(self) -> str:
        """Вычисляет SHA-256 отпечаток открытого ключа."""
        der = self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return f"sha256:{hashlib.sha256(der).hexdigest()}"

    def get_certificate_pem(self) -> str:
        """Возвращает валидный X.509 сертификат в формате PEM."""
        return self._cert_pem

    def sign_digest(self, canonical_bytes: bytes, scheme: str = "PSS") -> str:
        """
        Подписывает канонические байты.

        Args:
            canonical_bytes: Байты для подписи.
            scheme: 'PSS' (по умолчанию) или 'PKCS1'.
        """
        if scheme == "PKCS1":
            raw_sig = self._private_key.sign(
                canonical_bytes,
                padding.PKCS1v15(),
                hashes.SHA256()
            )
        else:
            raw_sig = self._private_key.sign(
                canonical_bytes,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH
                ),
                hashes.SHA256()
            )
        return base64.b64encode(raw_sig).decode("ascii")
