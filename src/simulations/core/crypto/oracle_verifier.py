import base64
import os
from typing import Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.simulations.core.utils.console_logger import log_error, log_warning


class OracleKeyManager:
    """
    Менеджер ключей доверенного оракула внешних событий смарт-контрактов.
    """

    def __init__(self, public_key_pem: Optional[str] = None):
        self._public_key_pem = public_key_pem or os.getenv("ORACLE_PUBLIC_KEY_PEM")
        self._dev_private_key = None

        if not self._public_key_pem:
            # Генерация ключа для локальных тестов / dev окружения
            key = rsa.generate_private_key(
                public_exponent=65537,
                key_size=2048,
                backend=default_backend(),
            )
            self._dev_private_key = key
            self._public_key_pem = key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode("utf-8")

    def get_public_key_pem(self) -> str:
        return self._public_key_pem

    def get_dev_private_key(self) -> Optional[rsa.RSAPrivateKey]:
        return self._dev_private_key

    def load_public_key(self) -> rsa.RSAPublicKey:
        return serialization.load_pem_public_key(
            self._public_key_pem.encode("utf-8"),
            backend=default_backend(),
        )


class OracleVerifier:
    """
    Проверяет цифровую подпись оракула при обновлении внешних условий смарт-контрактов.
    """

    def __init__(self, key_manager: Optional[OracleKeyManager] = None):
        self.key_manager = key_manager or OracleKeyManager()

    @staticmethod
    def get_canonical_payload(contract_id: str, status: str, oracle_name: Optional[str]) -> bytes:
        name = oracle_name or "default_oracle"
        return f"oracle_update:contract={contract_id}:status={status}:oracle={name}".encode("utf-8")

    def sign_condition(
        self,
        contract_id: str,
        status: str,
        oracle_name: Optional[str] = None,
        private_key: Optional[rsa.RSAPrivateKey] = None,
    ) -> str:
        """
        Создает цифровую подпись оракула (для тестирования или сервиса оракула).
        """
        key = private_key or self.key_manager.get_dev_private_key()
        if not key:
            raise RuntimeError("Приватный ключ оракула недоступен для создания подписи")

        payload = self.get_canonical_payload(contract_id, status, oracle_name)
        signature = key.sign(
            payload,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")

    def verify_condition_signature(
        self,
        contract_id: str,
        status: str,
        oracle_name: Optional[str],
        signature_b64: str,
    ) -> Tuple[bool, str]:
        """
        Проверяет подпись оракула под данными обновления контракта.
        """
        if not signature_b64 or not signature_b64.strip():
            return False, "Подпись оракула отсутствует"

        try:
            sig_bytes = base64.b64decode(signature_b64.strip().encode("ascii"))
        except Exception:
            return False, "Неверный base64 формат подписи оракула"

        payload = self.get_canonical_payload(contract_id, status, oracle_name)

        try:
            public_key = self.key_manager.load_public_key()
            public_key.verify(
                sig_bytes,
                payload,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH,
                ),
                hashes.SHA256(),
            )
            return True, "Подпись оракула валидна"
        except InvalidSignature:
            return False, "Цифровая подпись оракула недействительна"
        except Exception as e:
            return False, f"Ошибка проверки подписи оракула: {e}"


# Синглтон оракула
_ORACLE_VERIFIER_INSTANCE: Optional[OracleVerifier] = None


def get_oracle_verifier() -> OracleVerifier:
    global _ORACLE_VERIFIER_INSTANCE
    if _ORACLE_VERIFIER_INSTANCE is None:
        _ORACLE_VERIFIER_INSTANCE = OracleVerifier()
    return _ORACLE_VERIFIER_INSTANCE
