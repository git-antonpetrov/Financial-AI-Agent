"""
Единый фасад криптографической подписи и проверки квитанций для Admin Client (Zero-Trust Signer).
"""

import os
import time
import uuid
import json
import base64
import hashlib
from typing import Optional, Any, Union

from cryptography.hazmat.primitives.asymmetric import rsa, ec, padding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography import x509
from cryptography.exceptions import InvalidSignature

from .canonical import compute_rag_canonical_digest, compute_receipt_canonical_bytes
from .hardware_bridge import HardwareSigningBridge, SimulatedTPMBridge
from .software_bridge import SoftwareSigningBridge


class ZeroTrustClientSigner:
    """
    Управляет клиентской криптографической подписью запросов к RAG API и валидацией квитанций сервера.
    Поддерживает режимы:
    - 'auto': Автоматическое определение (TPM -> Fallback к Software / Simulated);
    - 'hardware': Строго аппаратный TPM 2.0 (Windows CNG);
    - 'simulated_tpm': Эмуляция TPM 2.0 / Windows Hello с подтверждением биометрии;
    - 'software': Программные ключи (admin_client.key / admin_client.crt).
    """

    def __init__(
        self,
        mode: str = "auto",
        cert_path: Optional[str] = None,
        key_path: Optional[str] = None,
        ca_cert_path: Optional[str] = None,
        key_pem: Optional[str] = None,
        cert_pem: Optional[str] = None,
        ca_cert_pem: Optional[str] = None,
        signing_scheme: str = "PSS"
    ):
        self.mode = mode
        self.signing_scheme = signing_scheme
        self.cert_path = cert_path
        self.key_path = key_path
        self.ca_cert_path = ca_cert_path
        self._ca_cert_pem = ca_cert_pem

        self._active_bridge: Union[HardwareSigningBridge, SimulatedTPMBridge, SoftwareSigningBridge]

        if mode == "hardware":
            if not HardwareSigningBridge.is_tpm_available():
                raise RuntimeError("Аппаратный криптомодуль TPM 2.0 недоступен на данном хосте")
            # Для практического использования без регистрации в реестре Windows оборачиваем в изолированный мост
            self._active_bridge = HardwareSigningBridge()
        elif mode == "simulated_tpm":
            self._active_bridge = SimulatedTPMBridge()
        elif mode == "software":
            self._active_bridge = SoftwareSigningBridge(
                cert_path=cert_path,
                key_path=key_path,
                key_pem=key_pem,
                cert_pem=cert_pem
            )
        else:  # 'auto'
            # Если переданы пути к файлам ключей на диске, используем Software
            if (key_path and os.path.exists(key_path)) or key_pem:
                self._active_bridge = SoftwareSigningBridge(
                    cert_path=cert_path,
                    key_path=key_path,
                    key_pem=key_pem,
                    cert_pem=cert_pem
                )
            elif HardwareSigningBridge.is_tpm_available():
                # Если TPM аппаратно доступен в системе Windows
                self._active_bridge = SimulatedTPMBridge(key_name="Enterprise_TPM_Workstation")
            else:
                self._active_bridge = SoftwareSigningBridge(
                    cert_path=cert_path,
                    key_path=key_path,
                    key_pem=key_pem,
                    cert_pem=cert_pem
                )

    def get_certificate_pem(self) -> str:
        """Возвращает клиентский X.509 сертификат в формате PEM."""
        return self._active_bridge.get_certificate_pem()

    def get_public_key_pem(self) -> str:
        """Возвращает открытый ключ клиента в формате PEM."""
        return self._active_bridge.get_public_key_pem()

    def get_key_fingerprint(self) -> str:
        """Вычисляет SHA-256 отпечаток открытого ключа клиента."""
        pub_pem = self.get_public_key_pem()
        der = serialization.load_pem_public_key(pub_pem.encode("utf-8")).public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return f"sha256:{hashlib.sha256(der).hexdigest()}"

    def sign(self, canonical_bytes: bytes) -> str:
        """Подписывает канонические байты и возвращает подпись в Base64."""
        return self._active_bridge.sign_digest(canonical_bytes, scheme=self.signing_scheme)

    def create_rag_request_headers(
        self,
        body_bytes: bytes,
        nonce: Optional[str] = None,
        timestamp: Optional[int] = None
    ) -> dict[str, str]:
        """
        Формирует защитные заголовки Zero-Trust для отправки в POST /api/v1/rag/documents:
        - X-Timestamp (Unix Epoch);
        - X-Nonce (одноразовый случайный идентификатор);
        - X-Signature (подпись канонического дайджеста);
        - X-Cert (клиентский сертификат).
        """
        req_ts = timestamp if timestamp is not None else int(time.time())
        req_nonce = nonce if nonce is not None else uuid.uuid4().hex

        canonical = compute_rag_canonical_digest(
            timestamp=req_ts,
            nonce=req_nonce,
            body_bytes=body_bytes
        )

        sig_b64 = self.sign(canonical)

        return {
            "X-Timestamp": str(req_ts),
            "X-Nonce": req_nonce,
            "X-Signature": sig_b64,
            "X-Cert": self.get_certificate_pem(),
        }

    def verify_server_receipt(
        self,
        receipt_data: dict[str, Any],
        server_signature_b64: str,
        trusted_server_cert_or_pubkey: Optional[str] = None
    ) -> bool:
        """
        Криптографически проверяет подлинность квитанции сервера (AckReceipt).
        
        Args:
            receipt_data: Словарь с данными квитанции.
            server_signature_b64: Цифровая подпись сервера в Base64.
            trusted_server_cert_or_pubkey: Открытый ключ или сертификат сервера/Root CA.
        """
        if not server_signature_b64 or not server_signature_b64.strip():
            return False

        # Разрешение публичного ключа сервера
        pub_key_source = trusted_server_cert_or_pubkey or self._ca_cert_pem
        if not pub_key_source and self.ca_cert_path and os.path.exists(self.ca_cert_path):
            try:
                with open(self.ca_cert_path, "r", encoding="utf-8") as f:
                    pub_key_source = f.read().strip()
            except Exception:
                pass

        if not pub_key_source:
            return False

        canonical_receipt = compute_receipt_canonical_bytes(receipt_data)

        try:
            raw_sig = base64.b64decode(server_signature_b64.strip())
            pem_bytes = pub_key_source.strip().encode("utf-8")

            pub_key = None
            if b"-----BEGIN CERTIFICATE-----" in pem_bytes:
                cert = x509.load_pem_x509_certificate(pem_bytes)
                pub_key = cert.public_key()
            elif b"-----BEGIN PUBLIC KEY-----" in pem_bytes or b"-----BEGIN RSA PUBLIC KEY-----" in pem_bytes:
                pub_key = serialization.load_pem_public_key(pem_bytes)

            if pub_key is None:
                return False

            if isinstance(pub_key, rsa.RSAPublicKey):
                # Пробуем RSA-PSS (Max Salt)
                try:
                    pub_key.verify(
                        raw_sig,
                        canonical_receipt,
                        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
                        hashes.SHA256()
                    )
                    return True
                except InvalidSignature:
                    pass

                # Пробуем RSA-PSS (Digest Salt)
                try:
                    pub_key.verify(
                        raw_sig,
                        canonical_receipt,
                        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                        hashes.SHA256()
                    )
                    return True
                except InvalidSignature:
                    pass

                # Пробуем PKCS#1 v1.5
                try:
                    pub_key.verify(
                        raw_sig,
                        canonical_receipt,
                        padding.PKCS1v15(),
                        hashes.SHA256()
                    )
                    return True
                except InvalidSignature:
                    pass

            elif isinstance(pub_key, ec.EllipticCurvePublicKey):
                try:
                    pub_key.verify(raw_sig, canonical_receipt, ec.ECDSA(hashes.SHA256()))
                    return True
                except InvalidSignature:
                    pass

            return False
        except Exception:
            return False
