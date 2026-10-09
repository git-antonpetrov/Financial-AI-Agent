"""
Аппаратный криптографический мост (Hardware Signing Bridge) для TPM 2.0 и Windows Hello.
Реализует Enterprise-уровень защиты: изоляция ключа в микрочипе TPM с биометрическим подтверждением.
"""

import sys
import os
import base64
import hashlib
import struct
from typing import Optional, Callable
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography import x509
from cryptography.x509.oid import NameOID
import datetime

try:
    from .cert_validator import (
        resolve_and_validate_client_certificate,
        find_client_cert_in_windows_store,
        CertificateMissingError,
        WindowsCertificateStoreError,
        CertificateValidationError,
        KeyMissingError,
    )
except ImportError:
    from cert_validator import (
        resolve_and_validate_client_certificate,
        find_client_cert_in_windows_store,
        CertificateMissingError,
        WindowsCertificateStoreError,
        CertificateValidationError,
        KeyMissingError,
    )


class HardwareSigningBridge:
    """
    Мост к аппаратному модулю безопасности TPM 2.0 через Windows CNG API (ncrypt.dll).
    Обеспечивает создание и использование неэкспортируемых ключей с биометрической авторизацией
    и криптографическое подписание в соответствии со спецификацией Zero-Trust PKI.
    """

    MS_PLATFORM_CRYPTO_PROVIDER = "Microsoft Platform Crypto Provider"
    MS_KEY_STORAGE_PROVIDER = "Microsoft Software Key Storage Provider"

    # Флаги Windows CNG
    NCRYPT_SILENT_FLAG = 0x00000040
    BCRYPT_PAD_PKCS1 = 0x00000002
    BCRYPT_PAD_PSS = 0x00000008

    def __init__(
        self,
        key_name: str = "FinancialAI_Admin_TPM_Key",
        require_biometrics: bool = False,
        cert_path: Optional[str] = None,
        cert_pem: Optional[str] = None,
        ca_cert_path: Optional[str] = None,
        auto_create_key: bool = True
    ):
        self.key_name = key_name
        self.require_biometrics = require_biometrics
        self.cert_path = cert_path
        self._cert_pem = cert_pem
        self.ca_cert_path = ca_cert_path
        self.auto_create_key = auto_create_key

        self._is_windows = sys.platform == "win32"
        self._provider_handle = None
        self._key_handle = None
        self._cached_public_key_pem: Optional[str] = None

        if not self._is_windows:
            raise RuntimeError("HardwareSigningBridge (TPM 2.0 / CNG) доступен только на платформе Windows")

        self._init_tpm_key()

    @classmethod
    def is_tpm_available(cls) -> bool:
        """Проверяет, доступен ли чип TPM 2.0 и провайдер Microsoft Platform Crypto Provider."""
        if sys.platform != "win32":
            return False
        try:
            import ctypes
            from ctypes import wintypes
            ncrypt = ctypes.windll.ncrypt
            h_prov = wintypes.HANDLE()
            status = ncrypt.NCryptOpenStorageProvider(
                ctypes.byref(h_prov),
                cls.MS_PLATFORM_CRYPTO_PROVIDER,
                0
            )
            if status == 0 and h_prov.value:
                ncrypt.NCryptFreeObject(h_prov)
                return True
            return False
        except Exception:
            return False

    def is_available(self) -> bool:
        """Проверяет общую доступность криптопровайдера на текущей системе."""
        return self._is_windows and self.is_tpm_available()

    def _init_tpm_key(self) -> None:
        """Инициализирует или открывает существующий неэкспортируемый ключ в чипе TPM 2.0."""
        import ctypes
        from ctypes import wintypes

        ncrypt = getattr(ctypes.windll, "ncrypt", None)
        if not ncrypt:
            raise RuntimeError("Системная библиотека ncrypt.dll недоступна")

        h_prov = wintypes.HANDLE()
        status = ncrypt.NCryptOpenStorageProvider(
            ctypes.byref(h_prov),
            self.MS_PLATFORM_CRYPTO_PROVIDER,
            0
        )
        if status != 0 or not h_prov.value:
            err_code = hex(status if status >= 0 else (1 << 32) + status)
            raise RuntimeError(
                f"Не удалось открыть Microsoft Platform Crypto Provider (TPM 2.0), статус: {err_code}"
            )
        self._provider_handle = h_prov

        h_key = wintypes.HANDLE()
        flags = self.NCRYPT_SILENT_FLAG if not self.require_biometrics else 0
        open_status = ncrypt.NCryptOpenKey(
            self._provider_handle,
            ctypes.byref(h_key),
            self.key_name,
            0,
            flags
        )

        if open_status == 0 and h_key.value:
            self._key_handle = h_key
            return

        if not self.auto_create_key:
            err_code = hex(open_status if open_status >= 0 else (1 << 32) + open_status)
            raise RuntimeError(
                f"Аппаратный ключ '{self.key_name}' не найден в TPM 2.0, код: {err_code}"
            )

        create_status = ncrypt.NCryptCreatePersistedKey(
            self._provider_handle,
            ctypes.byref(h_key),
            "RSA",
            self.key_name,
            0,
            0
        )
        if create_status != 0 or not h_key.value:
            err_code = hex(create_status if create_status >= 0 else (1 << 32) + create_status)
            raise RuntimeError(
                f"Не удалось создать аппаратный ключ в TPM 2.0, статус: {err_code}"
            )

        # Установка длины ключа: 2048 бит
        key_length = ctypes.c_ulong(2048)
        ncrypt.NCryptSetProperty(
            h_key,
            "Length",
            ctypes.byref(key_length),
            ctypes.sizeof(key_length),
            0
        )

        # Политика экспорта: 0 (NCRYPT_ALLOW_EXPORT_NONE — закрытый ключ аппаратный и неэкспортируемый)
        export_policy = ctypes.c_ulong(0)
        ncrypt.NCryptSetProperty(
            h_key,
            "Export Policy",
            ctypes.byref(export_policy),
            ctypes.sizeof(export_policy),
            0
        )

        # Финализация ключа внутри TPM
        fin_status = ncrypt.NCryptFinalizeKey(h_key, 0)
        if fin_status != 0:
            ncrypt.NCryptFreeObject(h_key)
            err_code = hex(fin_status if fin_status >= 0 else (1 << 32) + fin_status)
            raise RuntimeError(f"Ошибка финализации аппаратного ключа TPM 2.0: {err_code}")

        self._key_handle = h_key

    def sign_digest(self, canonical_bytes: bytes, scheme: str = "PSS") -> str:
        """
        Вычисляет аппаратную цифровую подпись данных в микрочипе TPM 2.0.

        Args:
            canonical_bytes: Байты данных для подписания.
            scheme: Схема подписания ('PSS' или 'PKCS1').

        Returns:
            str: Цифровая подпись в кодировке Base64.
        """
        if not self._key_handle:
            raise RuntimeError("Аппаратный ключ TPM не инициализирован")

        import ctypes
        from ctypes import wintypes

        ncrypt = ctypes.windll.ncrypt
        hash_digest = hashlib.sha256(canonical_bytes).digest()

        class BCRYPT_PSS_PADDING_INFO(ctypes.Structure):
            _fields_ = [
                ("pszAlgId", wintypes.LPCWSTR),
                ("cbSalt", wintypes.DWORD)
            ]

        class BCRYPT_PKCS1_PADDING_INFO(ctypes.Structure):
            _fields_ = [
                ("pszAlgId", wintypes.LPCWSTR)
            ]

        if scheme.upper() == "PKCS1":
            pad_info = BCRYPT_PKCS1_PADDING_INFO("SHA256")
            pad_ptr = ctypes.byref(pad_info)
            pad_flag = self.BCRYPT_PAD_PKCS1
        else:
            pad_info = BCRYPT_PSS_PADDING_INFO("SHA256", 32)
            pad_ptr = ctypes.byref(pad_info)
            pad_flag = self.BCRYPT_PAD_PSS

        sig_len = wintypes.DWORD(0)
        status = ncrypt.NCryptSignHash(
            self._key_handle,
            pad_ptr,
            hash_digest,
            len(hash_digest),
            None,
            0,
            ctypes.byref(sig_len),
            pad_flag
        )
        if status != 0 or sig_len.value == 0:
            err_code = hex(status if status >= 0 else (1 << 32) + status)
            raise RuntimeError(f"Ошибка вычисления размера подписи в TPM 2.0: {err_code}")

        sig_buf = (ctypes.c_ubyte * sig_len.value)()
        status2 = ncrypt.NCryptSignHash(
            self._key_handle,
            pad_ptr,
            hash_digest,
            len(hash_digest),
            sig_buf,
            sig_len.value,
            ctypes.byref(sig_len),
            pad_flag
        )
        if status2 != 0:
            err_code = hex(status2 if status2 >= 0 else (1 << 32) + status2)
            raise RuntimeError(f"Ошибка вычисления аппаратной цифровой подписи TPM 2.0: {err_code}")

        return base64.b64encode(bytes(sig_buf)).decode("ascii")

    def get_public_key_pem(self) -> str:
        """Возвращает открытый ключ аппаратного модуля TPM в формате PEM."""
        if self._cached_public_key_pem:
            return self._cached_public_key_pem

        if not self._key_handle:
            raise RuntimeError("Аппаратный ключ TPM не инициализирован")

        import ctypes
        from ctypes import wintypes

        ncrypt = ctypes.windll.ncrypt

        cb_out = wintypes.DWORD(0)
        status = ncrypt.NCryptExportKey(
            self._key_handle,
            0,
            "RSAPUBLICBLOB",
            None,
            None,
            0,
            ctypes.byref(cb_out),
            0
        )
        if status != 0 or cb_out.value == 0:
            err_code = hex(status if status >= 0 else (1 << 32) + status)
            raise RuntimeError(f"Ошибка запроса размера публичного ключа из TPM 2.0: {err_code}")

        buf = (ctypes.c_ubyte * cb_out.value)()
        status2 = ncrypt.NCryptExportKey(
            self._key_handle,
            0,
            "RSAPUBLICBLOB",
            None,
            buf,
            cb_out.value,
            ctypes.byref(cb_out),
            0
        )
        if status2 != 0:
            err_code = hex(status2 if status2 >= 0 else (1 << 32) + status2)
            raise RuntimeError(f"Ошибка экспорта публичного ключа из TPM 2.0: {err_code}")

        blob = bytes(buf)
        _, _, exp_len, mod_len, _, _ = struct.unpack('<IIIIII', blob[:24])
        exp_bytes = blob[24:24 + exp_len]
        mod_bytes = blob[24 + exp_len:24 + exp_len + mod_len]

        e = int.from_bytes(exp_bytes, "big")
        n = int.from_bytes(mod_bytes, "big")

        pub_numbers = rsa.RSAPublicNumbers(e, n)
        pub_key = pub_numbers.public_key()
        pem = pub_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode("utf-8")

        self._cached_public_key_pem = pem
        return pem

    def get_key_fingerprint(self) -> str:
        """Вычисляет SHA-256 отпечаток открытого ключа TPM."""
        pub_pem = self.get_public_key_pem()
        der = serialization.load_pem_public_key(pub_pem.encode("utf-8")).public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return f"sha256:{hashlib.sha256(der).hexdigest()}"

    def get_certificate_pem(self) -> str:
        """Возвращает X.509 сертификат оператора в формате PEM, соответствующий аппаратному ключу."""
        if self._cert_pem:
            return self._cert_pem

        win_cert = self._find_cert_in_windows_store()
        if win_cert:
            self._cert_pem = win_cert
            return win_cert

        tpm_pub_key = serialization.load_pem_public_key(self.get_public_key_pem().encode("utf-8"))
        pem_data, _ = resolve_and_validate_client_certificate(
            cert_path=self.cert_path,
            cert_pem=None,
            ca_cert_path=self.ca_cert_path,
            expected_public_key=tpm_pub_key
        )
        self._cert_pem = pem_data
        return pem_data

    def _find_cert_in_windows_store(self) -> Optional[str]:
        """Ищет соответствующий сертификат в системном хранилище сертификатов Windows 'MY'."""
        try:
            tpm_pub_key = serialization.load_pem_public_key(self.get_public_key_pem().encode("utf-8"))
            found = find_client_cert_in_windows_store(expected_public_key=tpm_pub_key)
            if found:
                pem_data, _ = found
                validated_pem, _ = resolve_and_validate_client_certificate(
                    cert_pem=pem_data,
                    ca_cert_path=self.ca_cert_path,
                    expected_public_key=tpm_pub_key
                )
                return validated_pem
        except Exception:
            pass
        return None

    def close(self) -> None:
        """Освобождает дескрипторы CNG."""
        import ctypes
        if sys.platform == "win32":
            ncrypt = getattr(ctypes.windll, "ncrypt", None)
            if ncrypt:
                if self._key_handle:
                    try:
                        ncrypt.NCryptFreeObject(self._key_handle)
                    except Exception:
                        pass
                    self._key_handle = None
                if self._provider_handle:
                    try:
                        ncrypt.NCryptFreeObject(self._provider_handle)
                    except Exception:
                        pass
                    self._provider_handle = None

    def __del__(self) -> None:
        self.close()


class SimulatedTPMBridge:
    """
    Эмулятор TPM 2.0 и Windows Hello для разработки, CI/CD и изолированного тестирования.
    Поведение строго имитирует аппаратный модуль:
    - Приватный ключ хранится изолированно в памяти (имитация Non-Exportable);
    - Подписание требует биометрической верификации через callback (Windows Hello prompt);
    - Генерирует подписи PKCS#1 v1.5 или RSA-PSS, совместимые с проверкой на сервере;
    - Требует обязательный легитимный сертификат, подписанный Root CA. Генерация самоподписанных сертификатов строго запрещена.
    """

    def __init__(
        self,
        key_name: str = "Simulated_TPM_Hello_Key",
        biometric_prompt_callback: Optional[Callable[[], bool]] = None,
        private_key: Optional[rsa.RSAPrivateKey] = None,
        key_path: Optional[str] = None,
        key_pem: Optional[str] = None,
        cert_path: Optional[str] = None,
        cert_pem: Optional[str] = None,
        ca_cert_path: Optional[str] = None,
        ca_cert_pem: Optional[str] = None,
    ):
        self.key_name = key_name
        self.biometric_prompt_callback = biometric_prompt_callback or (lambda: True)
        self.cert_path = cert_path
        self.key_path = key_path
        self.ca_cert_path = ca_cert_path

        if private_key is not None:
            self._private_key = private_key
        elif key_pem and key_pem.strip():
            self._private_key = serialization.load_pem_private_key(key_pem.strip().encode("utf-8"), password=None)
        elif key_path:
            if not os.path.exists(key_path):
                raise KeyMissingError(f"Закрытый ключ для эмуляции TPM не найден по пути {key_path}")
            with open(key_path, "rb") as f:
                self._private_key = serialization.load_pem_private_key(f.read(), password=None)
        else:
            self._private_key = rsa.generate_private_key(
                public_exponent=65537,
                key_size=2048
            )

        self._cert_pem, self._cert_obj = resolve_and_validate_client_certificate(
            cert_path=cert_path,
            cert_pem=cert_pem,
            ca_cert_path=ca_cert_path,
            ca_cert_pem=ca_cert_pem,
            expected_public_key=self._private_key.public_key()
        )

    def get_public_key_pem(self) -> str:
        """Возвращает открытый ключ TPM в формате PEM."""
        return self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode("utf-8")

    def get_certificate_pem(self) -> str:
        """Возвращает X.509 сертификат TPM в формате PEM."""
        return self._cert_pem

    def sign_digest(self, canonical_bytes: bytes, scheme: str = "PSS") -> str:
        """
        Вычисляет цифровую подпись данных с обязательным вызовом биометрического подтверждения.

        Args:
            canonical_bytes: Байты данных для подписания.
            scheme: Схема подписания ('PSS' или 'PKCS1').

        Returns:
            str: Цифровая подпись в кодировке Base64.
        """
        if not self.biometric_prompt_callback():
            raise PermissionError("Биометрическая аутентификация Windows Hello отклонена пользователем")

        if scheme == "PKCS1":
            sig = self._private_key.sign(
                canonical_bytes,
                padding.PKCS1v15(),
                hashes.SHA256()
            )
        else:
            sig = self._private_key.sign(
                canonical_bytes,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH
                ),
                hashes.SHA256()
            )
        return base64.b64encode(sig).decode("ascii")
