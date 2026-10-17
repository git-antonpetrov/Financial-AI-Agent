import base64
import os
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.simulations.core.crypto.enclave_verifier import NonceStore


class OracleKeyManager:
    """
    Менеджер ключей доверенного оракула внешних событий смарт-контрактов.
    В строгом соответствии с моделью Zero-Trust (SEC-13), генерация случайных
    ключей в оперативной памяти запрещена. Ключи загружаются из настроенных путей/переменных;
    при их отсутствии выбрасывается RuntimeError.
    """

    def __init__(
        self,
        public_key_pem: str | None = None,
        private_key_pem: str | None = None,
    ):
        self._public_key_pem = public_key_pem or os.getenv("ORACLE_PUBLIC_KEY_PEM")
        self._private_key_pem = private_key_pem or os.getenv("ORACLE_PRIVATE_KEY_PEM")
        self._private_key = None

        if not self._public_key_pem:
            pub_paths = [
                os.getenv("ORACLE_PUBLIC_KEY_PATH", ""),
                os.getenv("ORACLE_KEY_PATH", ""),
                "/certs/oracle.pub",
                "/certs/oracle_public.pem",
                "/certs/oracle.crt",
            ]
            for p in pub_paths:
                if p and os.path.isfile(p):
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            self._public_key_pem = f.read()
                            break
                    except Exception:
                        pass

        if not self._public_key_pem:
            raise RuntimeError(
                "Публичный ключ оракула не найден (ORACLE_PUBLIC_KEY_PEM / ORACLE_PUBLIC_KEY_PATH). "
                "Генерация случайных ключей в оперативной памяти запрещена моделью Zero-Trust."
            )

        if not self._private_key_pem:
            priv_paths = [
                os.getenv("ORACLE_PRIVATE_KEY_PATH", ""),
                "/certs/oracle.key",
                "/certs/oracle_private.pem",
            ]
            for p in priv_paths:
                if p and os.path.isfile(p):
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            self._private_key_pem = f.read()
                            break
                    except Exception:
                        pass

        if self._private_key_pem:
            try:
                self._private_key = serialization.load_pem_private_key(
                    self._private_key_pem.encode("utf-8"),
                    password=None,
                    backend=default_backend(),
                )
            except Exception:
                pass

    def get_public_key_pem(self) -> str:
        return self._public_key_pem

    def get_private_key(self) -> rsa.RSAPrivateKey | None:
        return self._private_key

    def load_public_key(self) -> rsa.RSAPublicKey:
        return serialization.load_pem_public_key(
            self._public_key_pem.encode("utf-8"),
            backend=default_backend(),
        )


class OracleVerifier:
    """
    Проверяет цифровую подпись оракула при обновлении внешних условий смарт-контрактов
    с защитой от атак повторного воспроизведения (Replay Attacks) через NonceStore и Timestamp (SEC-14).
    """

    def __init__(
        self,
        key_manager: OracleKeyManager | None = None,
        nonce_store: NonceStore | None = None,
        max_drift_seconds: int = 300,
    ):
        self.key_manager = key_manager or OracleKeyManager()
        self.nonce_store = nonce_store or NonceStore()
        self.max_drift_seconds = max_drift_seconds

    @staticmethod
    def get_canonical_payload(
        contract_id: str,
        status: str,
        oracle_name: str | None,
        nonce: str | None = None,
        timestamp: float | str | None = None,
    ) -> bytes:
        name = oracle_name or "default_oracle"
        if nonce is not None or timestamp is not None:
            ts_str = str(int(float(timestamp))) if timestamp is not None else ""
            nonce_str = str(nonce) if nonce is not None else ""
            payload = (
                f"oracle_update:contract={contract_id}:status={status}:oracle={name}:ts={ts_str}:nonce={nonce_str}"
            )
            return payload.encode()
        return f"oracle_update:contract={contract_id}:status={status}:oracle={name}".encode()

    def sign_condition(
        self,
        contract_id: str,
        status: str,
        oracle_name: str | None = None,
        private_key: rsa.RSAPrivateKey | None = None,
        nonce: str | None = None,
        timestamp: float | str | None = None,
    ) -> str:
        """
        Создает цифровую подпись оракула (для тестирования или сервиса оракула).
        """
        key = private_key or self.key_manager.get_private_key()
        if not key:
            raise RuntimeError("Приватный ключ оракула недоступен для создания подписи")

        payload = self.get_canonical_payload(contract_id, status, oracle_name, nonce=nonce, timestamp=timestamp)
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
        oracle_name: str | None,
        signature_b64: str,
        nonce: str | None = None,
        timestamp: float | str | None = None,
    ) -> tuple[bool, str]:
        """
        Проверяет подпись оракула под данными обновления контракта с защитой от Replay-атак (SEC-14).
        """
        if not signature_b64 or not signature_b64.strip():
            return False, "Подпись оракула отсутствует"

        # SEC-14: Для верификации подписи оракула строго обязательны параметры timestamp и nonce (защита от Replay)
        if timestamp is None or nonce is None:
            return False, "Для верификации подписи оракула обязательны параметры timestamp и nonce (SEC-14)"

        # SEC-14: Проверка дрейфа времени (Timestamp Skew)
        if timestamp is not None:
            try:
                ts_val = float(timestamp)
                now = time.time()
                drift = abs(now - ts_val)
                if drift > self.max_drift_seconds:
                    return (
                        False,
                        f"Метка времени оракула устарела (дрейф {drift:.1f}с > {self.max_drift_seconds}с)",
                    )
            except (ValueError, TypeError):
                return False, "Некорректный формат метки времени timestamp"

        # SEC-14: Проверка одноразового nonce для предотвращения Replay-атак
        if nonce is not None:
            if not self.nonce_store.record_nonce_if_new(nonce):
                return (
                    False,
                    f"Обнаружена Replay-атака: данный nonce оракула '{nonce}' уже был использован",
                )

        try:
            sig_bytes = base64.b64decode(signature_b64.strip().encode("ascii"))
        except Exception:
            return False, "Неверный base64 формат подписи оракула"

        try:
            public_key = self.key_manager.load_public_key()
            payload = self.get_canonical_payload(contract_id, status, oracle_name, nonce=nonce, timestamp=timestamp)
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
            if nonce is not None:
                self.nonce_store.discard_nonce(nonce)
            return False, "Цифровая подпись оракула недействительна"
        except Exception as e:
            if nonce is not None:
                self.nonce_store.discard_nonce(nonce)
            return False, f"Ошибка проверки подписи оракула: {e}"


# Синглтон оракула
_ORACLE_VERIFIER_INSTANCE: OracleVerifier | None = None


def get_oracle_verifier() -> OracleVerifier:
    global _ORACLE_VERIFIER_INSTANCE
    if _ORACLE_VERIFIER_INSTANCE is None:
        _ORACLE_VERIFIER_INSTANCE = OracleVerifier()
    return _ORACLE_VERIFIER_INSTANCE


def reset_oracle_verifier_for_tests() -> None:
    """Сбрасывает синглтон верификатора оракула для изоляции тестов."""
    global _ORACLE_VERIFIER_INSTANCE
    _ORACLE_VERIFIER_INSTANCE = None
