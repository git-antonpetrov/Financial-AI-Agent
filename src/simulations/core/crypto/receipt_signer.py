import base64
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.simulations.core.crypto.bank_ca import get_bank_ca, BankCertificateAuthority


def _canonical_json(data: Any) -> bytes:
    """
    Каноническая детерминированная сериализация данных в UTF-8 JSON.
    Сортировка ключей, отсутствие пробелов между разделителями.
    """
    def _default(obj):
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if hasattr(obj, "dict") and callable(obj.dict):
            return obj.dict()
        if hasattr(obj, "model_dump") and callable(obj.model_dump):
            return obj.model_dump()
        return str(obj)

    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_default,
    ).encode("utf-8")


class ReceiptSigner:
    """
    Формирует и криптографически подписывает чеки-квитанции (Signed Receipt)
    приватным ключом Банка в соответствии с пунктом 5 архитектуры arch.txt.
    """

    def __init__(self, ca: Optional[BankCertificateAuthority] = None):
        self.ca = ca or get_bank_ca()

    def create_and_sign_receipt(
        self,
        action: str,
        data: Dict[str, Any],
        request_nonce: Optional[str] = None,
        status: str = "completed",
    ) -> Dict[str, Any]:
        """
        Создает канонический чек операции и подписывает его приватным ключом Банка (RSA-PSS SHA-256).
        """
        if not self.ca.bank_private_key:
            raise RuntimeError("Приватный ключ банка недоступен для подписи чека")

        receipt_payload = {
            "receipt_id": str(uuid.uuid4()),
            "action": action,
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request_nonce": request_nonce,
            "data": data,
        }

        canonical_bytes = _canonical_json(receipt_payload)

        # Подпись с использованием RSA-PSS и SHA-256
        signature_bytes = self.ca.bank_private_key.sign(
            canonical_bytes,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )

        signature_b64 = base64.b64encode(signature_bytes).decode("ascii")

        return {
            "receipt": receipt_payload,
            "signature": signature_b64,
            "algorithm": "SHA256withRSA-PSS",
            "signer_cn": "Financial AI Simulation Bank",
            "cert_fingerprint": self.ca.get_bank_cert_fingerprint(),
            "bank_cert_pem": self.ca.get_bank_cert_pem(),
            "ca_cert_pem": self.ca.get_ca_cert_pem(),
        }

    @classmethod
    def verify_receipt(
        cls,
        signed_receipt: Dict[str, Any],
        trusted_ca_cert_pem: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Верифицирует подписанный чек Банка:
        1. Проверяет валидность сертификата Банка через доверенный Root CA.
        2. Проверяет цифровую подпись под каноническим представлением чека.
        """
        try:
            if not isinstance(signed_receipt, dict):
                return False, "Неверная структура чека: ожидается словарь"

            receipt = signed_receipt.get("receipt")
            signature_b64 = signed_receipt.get("signature")
            bank_cert_pem = signed_receipt.get("bank_cert_pem")

            if not receipt or not signature_b64 or not bank_cert_pem:
                return False, "Отсутствуют обязательные поля чека (receipt, signature или bank_cert_pem)"

            # 1. Проверка сертификата Банка через CA (если CA передан или берется дефолтный)
            if trusted_ca_cert_pem:
                if not BankCertificateAuthority.verify_certificate_against_ca(bank_cert_pem, trusted_ca_cert_pem):
                    return False, "Сертификат Банка не прошел валидацию доверенным Root CA"

            # 2. Загрузка открытого ключа Банка из сертификата
            bank_cert = x509.load_pem_x509_certificate(bank_cert_pem.encode("utf-8"), default_backend())
            public_key = bank_cert.public_key()

            if not isinstance(public_key, rsa.RSAPublicKey):
                return False, "Открытый ключ Банка не является RSA ключом"

            # 3. Восстановление канонических байтов чека
            canonical_bytes = _canonical_json(receipt)
            signature_bytes = base64.b64decode(signature_b64.encode("ascii"))

            # 4. Проверка подписи RSA-PSS
            public_key.verify(
                signature_bytes,
                canonical_bytes,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH,
                ),
                hashes.SHA256(),
            )
            return True, "Подпись чека успешно верифицирована"
        except InvalidSignature:
            return False, "Недействительная цифровая подпись чека (Invalid Signature)"
        except Exception as e:
            return False, f"Ошибка валидации подписи чека: {e}"


# Синглтон для быстрого доступа
_RECEIPT_SIGNER_INSTANCE: Optional[ReceiptSigner] = None


def get_receipt_signer() -> ReceiptSigner:
    global _RECEIPT_SIGNER_INSTANCE
    if _RECEIPT_SIGNER_INSTANCE is None:
        _RECEIPT_SIGNER_INSTANCE = ReceiptSigner(get_bank_ca())
    return _RECEIPT_SIGNER_INSTANCE
