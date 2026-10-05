import base64
import hashlib
import os
import threading
import time
from typing import Dict, Optional, Tuple, Union

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

from src.simulations.core.utils.console_logger import log_error, log_info, log_warning


class NonceStore:
    """
    Хранилище одноразовых nonce для защиты от атак повторного воспроизведения (Replay Attacks).
    Поддерживает Redis с автоматическим переключением на потокобезопасную память (TTL cache).
    """

    def __init__(self, redis_client=None, default_ttl: int = 300):
        self.default_ttl = default_ttl
        self.redis_client = redis_client
        self._memory_cache: Dict[str, float] = {}
        self._lock = threading.Lock()

    def record_nonce_if_new(self, nonce: str, ttl: Optional[int] = None) -> bool:
        """
        Пытается атомарно записать nonce.
        Возвращает True, если nonce уникален и успешно записан.
        Возвращает False, если nonce уже использовался (Replay Attack).
        """
        if not nonce or not nonce.strip():
            return False

        nonce = nonce.strip()
        actual_ttl = ttl or self.default_ttl

        # 1. Попытка через Redis, если подключен
        if self.redis_client is not None:
            try:
                key = f"enclave:nonce:{nonce}"
                # set nx=True возвращает True только если ключа не существовало
                success = self.redis_client.set(key, "1", ex=actual_ttl, nx=True)
                return bool(success)
            except Exception as e:
                log_warning("NonceStore", f"Redis недоступен для проверки nonce ({e}), откат к памяти")

        # 2. Потокобезопасная проверка в оперативной памяти
        now = time.time()
        with self._lock:
            # Очистка устаревших nonce
            expired_keys = [k for k, exp in self._memory_cache.items() if exp <= now]
            for k in expired_keys:
                del self._memory_cache[k]

            if nonce in self._memory_cache:
                return False  # Уже существует

            self._memory_cache[nonce] = now + actual_ttl
            return True

    def clear(self) -> None:
        """Очищает память (для тестов)."""
        with self._lock:
            self._memory_cache.clear()


class EnclaveKeyManager:
    """
    Менеджер открытого ключа Аппаратного Анклава (Enclave_PublicKey).
    В боевом режиме загружает сохраненный публичный ключ Анклава.
    В тестовом/dev режиме генерирует валидную пару ключей.
    """

    def __init__(self, public_key_pem: Optional[str] = None):
        self._public_key_pem = public_key_pem or os.getenv("ENCLAVE_PUBLIC_KEY_PEM")
        self._dev_private_key = None

        if not self._public_key_pem:
            # Автоматическая генерация для локальной разработки / тестов
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

    def get_dev_private_key_pem(self) -> Optional[str]:
        if self._dev_private_key:
            return self._dev_private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ).decode("utf-8")
        return None

    def load_public_key(self):
        return serialization.load_pem_public_key(
            self._public_key_pem.encode("utf-8"),
            backend=default_backend(),
        )


class EnclaveVerifier:
    """
    Криптографический верификатор запросов от Аппаратного Анклава (Блок 5 схемы arch.txt):
    1. Проверяет свежесть временной метки (Timestamp Skew).
    2. Проверяет одноразовый nonce на Replay Attack.
    3. Проверяет цифровую подпись Анклава по сохраненному Enclave_PublicKey.
    """

    def __init__(
        self,
        key_manager: Optional[EnclaveKeyManager] = None,
        nonce_store: Optional[NonceStore] = None,
        max_time_skew_seconds: int = 300,
    ):
        self.key_manager = key_manager or EnclaveKeyManager()
        self.nonce_store = nonce_store or NonceStore()
        self.max_time_skew_seconds = max_time_skew_seconds

    @staticmethod
    def compute_payload_digest(
        method: str,
        path: str,
        timestamp: Union[str, int, float],
        nonce: str,
        body_bytes: bytes,
    ) -> bytes:
        """
        Формирует канонический хэш запроса для цифровой подписи Анклава:
        SHA256(METHOD:PATH:TIMESTAMP:NONCE:SHA256(BODY))
        """
        body_sha256 = hashlib.sha256(body_bytes).hexdigest()
        canonical_str = f"{method.upper()}:{path}:{timestamp}:{nonce}:{body_sha256}"
        return canonical_str.encode("utf-8")

    def verify_request(
        self,
        method: str,
        path: str,
        timestamp_val: Union[str, int, float],
        nonce: str,
        body_bytes: bytes,
        signature_b64: str,
    ) -> Tuple[bool, str]:
        """
        Выполняет полную криптографическую валидацию входящего запроса от Анклава.
        """
        # 1. Проверка временной метки (Timestamp Skew)
        try:
            ts_float = float(timestamp_val)
        except (ValueError, TypeError):
            return False, "Неверный формат временной метки X-Timestamp"

        now = time.time()
        # Защита от запросов из далекого прошлого или далекого будущего
        if (now - ts_float) > self.max_time_skew_seconds:
            return False, f"Запрос устарел (превышен допустимый интервал {self.max_time_skew_seconds} с)"
        if (ts_float - now) > 60:
            return False, "Временная метка запроса указывает на будущее время (> 60 с)"

        # 2. Проверка уникальности nonce (Replay Attack)
        if not self.nonce_store.record_nonce_if_new(nonce, ttl=self.max_time_skew_seconds):
            return False, f"Обнаружена повторная атака (Replay Attack): nonce '{nonce}' уже был использован"

        # 3. Верификация цифровой подписи Анклава
        try:
            signature_bytes = base64.b64decode(signature_b64.encode("ascii"))
        except Exception:
            return False, "Неверный base64 формат цифровой подписи X-Enclave-Signature"

        canonical_data = self.compute_payload_digest(
            method=method,
            path=path,
            timestamp=timestamp_val,
            nonce=nonce,
            body_bytes=body_bytes,
        )

        try:
            public_key = self.key_manager.load_public_key()

            if isinstance(public_key, rsa.RSAPublicKey):
                # Пробуем PSS, при ошибке PKCS1v15 для совместимости
                try:
                    public_key.verify(
                        signature_bytes,
                        canonical_data,
                        padding.PSS(
                            mgf=padding.MGF1(hashes.SHA256()),
                            salt_length=padding.PSS.MAX_LENGTH,
                        ),
                        hashes.SHA256(),
                    )
                    return True, "Подпись анклава (RSA-PSS) валидна"
                except InvalidSignature:
                    public_key.verify(
                        signature_bytes,
                        canonical_data,
                        padding.PKCS1v15(),
                        hashes.SHA256(),
                    )
                    return True, "Подпись анклава (RSA-PKCS1v15) валидна"

            elif isinstance(public_key, ec.EllipticCurvePublicKey):
                public_key.verify(
                    signature_bytes,
                    canonical_data,
                    ec.ECDSA(hashes.SHA256()),
                )
                return True, "Подпись анклава (ECDSA) валидна"

            elif isinstance(public_key, ed25519.Ed25519PublicKey):
                public_key.verify(signature_bytes, canonical_data)
                return True, "Подпись анклава (Ed25519) валидна"

            else:
                return False, f"Неподдерживаемый тип публичного ключа анклава: {type(public_key)}"

        except InvalidSignature:
            return False, "Цифровая подпись Анклава недействительна (Invalid Signature)"
        except Exception as e:
            return False, f"Ошибка проверки подписи: {e}"


# Синглтон верификатора
_ENCLAVE_VERIFIER_INSTANCE: Optional[EnclaveVerifier] = None


def get_enclave_verifier() -> EnclaveVerifier:
    global _ENCLAVE_VERIFIER_INSTANCE
    if _ENCLAVE_VERIFIER_INSTANCE is None:
        _ENCLAVE_VERIFIER_INSTANCE = EnclaveVerifier()
    return _ENCLAVE_VERIFIER_INSTANCE
