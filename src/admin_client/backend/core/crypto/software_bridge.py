"""
Программный криптографический мост (Software Signing Bridge).
Используется как fallback в тестовом окружении или при отсутствии аппаратного токена TPM.
"""

import os
import base64
from typing import Optional, Union
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography import x509
from cryptography.x509.oid import NameOID
import datetime


class SoftwareSigningBridge:
    """
    Программное управление ключами и сертификатами для клиентской стороны.
    Загружает приватный ключ и сертификат с диска или генерирует их в оперативной памяти.
    """

    def __init__(
        self,
        cert_path: Optional[str] = None,
        key_path: Optional[str] = None,
        key_pem: Optional[str] = None,
        cert_pem: Optional[str] = None,
        key_password: Optional[str] = None,
    ):
        self.cert_path = cert_path
        self.key_path = key_path
        self.key_password = key_password
        
        self._private_key: Optional[rsa.RSAPrivateKey] = None
        self._cert_pem: Optional[str] = None
        
        if key_pem:
            try:
                self._private_key = self._load_private_key_pem(key_pem, key_password)
            except Exception:
                self._private_key = None
        elif key_path and os.path.exists(key_path):
            try:
                with open(key_path, "rb") as f:
                    self._private_key = self._load_private_key_pem(f.read().decode("utf-8"), key_password)
            except Exception:
                self._private_key = None
                
        if cert_pem:
            self._cert_pem = cert_pem.strip()
        elif cert_path and os.path.exists(cert_path):
            try:
                with open(cert_path, "r", encoding="utf-8") as f:
                    data = f.read().strip()
                    if "-----BEGIN CERTIFICATE-----" in data:
                        self._cert_pem = data
            except Exception:
                self._cert_pem = None
                
        # Если ключ не был предоставлен или не удалось прочитать, создаем пару в памяти для локального тестирования
        if self._private_key is None:
            self._private_key = rsa.generate_private_key(
                public_exponent=65537,
                key_size=2048
            )
            
        if self._cert_pem is None:
            self._cert_pem = self._generate_fallback_cert()

    def _load_private_key_pem(self, pem_str: str, password: Optional[str]) -> rsa.RSAPrivateKey:
        pwd = password.encode("utf-8") if password else None
        key = serialization.load_pem_private_key(pem_str.encode("utf-8"), password=pwd)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise TypeError("Поддерживаются только RSA приватные ключи")
        return key

    def _generate_fallback_cert(self) -> str:
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "FinancialAI Admin Client Software"),
            x509.NameAttribute(NameOID.COMMON_NAME, "admin-workstation-software"),
        ])
        cert = x509.CertificateBuilder().subject_name(
            subject
        ).issuer_name(
            issuer
        ).public_key(
            self._private_key.public_key()
        ).serial_number(
            int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        ).not_valid_before(
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
        ).not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
        ).sign(self._private_key, hashes.SHA256())

        return cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")

    def get_public_key_pem(self) -> str:
        return self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode("utf-8")

    def get_certificate_pem(self) -> str:
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
