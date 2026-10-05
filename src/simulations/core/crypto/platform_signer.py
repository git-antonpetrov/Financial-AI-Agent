import base64
from decimal import Decimal
from typing import Optional, Union
from datetime import datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.simulations.core.crypto.bank_ca import get_bank_ca, BankCertificateAuthority


class DigitalPlatformSigner:
    """
    Криптографический подписант транзакций платформы цифрового рубля.
    Заменяет фиктивные подписи (simulated_sig_...) на криптографически стойкие цифровые подписи (RSA-PSS).
    """

    def __init__(self, ca: Optional[BankCertificateAuthority] = None):
        self.ca = ca or get_bank_ca()

    @staticmethod
    def canonical_transaction_payload(
        sender_id: Union[str, int],
        receiver_id: Union[str, int],
        amount: Union[Decimal, str, float],
        contract_id: Optional[Union[str, int]] = None,
        timestamp: Optional[Union[datetime, str, int, float]] = None,
    ) -> bytes:
        amt_str = str(Decimal(str(amount)))
        c_id = str(contract_id) if contract_id is not None else "direct"
        ts_str = str(timestamp) if timestamp is not None else "0"
        payload_str = f"RUBLE_TX:sender={sender_id}:receiver={receiver_id}:amount={amt_str}:contract={c_id}:ts={ts_str}"
        return payload_str.encode("utf-8")

    def sign_transaction(
        self,
        sender_id: Union[str, int],
        receiver_id: Union[str, int],
        amount: Union[Decimal, str, float],
        contract_id: Optional[Union[str, int]] = None,
        timestamp: Optional[Union[datetime, str, int, float]] = None,
    ) -> str:
        """
        Формирует цифровую подпись транзакции цифрового рубля ключом оператора платформы.
        """
        if not self.ca.bank_private_key:
            raise RuntimeError("Приватный ключ платформы недоступен для подписи транзакции")

        payload = self.canonical_transaction_payload(
            sender_id=sender_id,
            receiver_id=receiver_id,
            amount=amount,
            contract_id=contract_id,
            timestamp=timestamp,
        )

        signature_bytes = self.ca.bank_private_key.sign(
            payload,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature_bytes).decode("ascii")

    def verify_transaction_signature(
        self,
        sender_id: Union[str, int],
        receiver_id: Union[str, int],
        amount: Union[Decimal, str, float],
        signature_b64: str,
        contract_id: Optional[Union[str, int]] = None,
        timestamp: Optional[Union[datetime, str, int, float]] = None,
    ) -> bool:
        """
        Проверяет подлинность транзакции по открытому ключу платформы Банка.
        """
        if not signature_b64 or not signature_b64.strip():
            return False

        try:
            sig_bytes = base64.b64decode(signature_b64.strip().encode("ascii"))
            payload = self.canonical_transaction_payload(
                sender_id=sender_id,
                receiver_id=receiver_id,
                amount=amount,
                contract_id=contract_id,
                timestamp=timestamp,
            )

            public_key = self.ca.bank_cert.public_key()
            if not isinstance(public_key, rsa.RSAPublicKey):
                return False

            public_key.verify(
                sig_bytes,
                payload,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH,
                ),
                hashes.SHA256(),
            )
            return True
        except (InvalidSignature, Exception):
            return False


# Синглтон подписанта
_DIGITAL_SIGNER_INSTANCE: Optional[DigitalPlatformSigner] = None


def get_digital_platform_signer() -> DigitalPlatformSigner:
    global _DIGITAL_SIGNER_INSTANCE
    if _DIGITAL_SIGNER_INSTANCE is None:
        _DIGITAL_SIGNER_INSTANCE = DigitalPlatformSigner()
    return _DIGITAL_SIGNER_INSTANCE
