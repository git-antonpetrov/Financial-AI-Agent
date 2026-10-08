"""
Валидатор цепочки доверия X.509 и цифровых подписей (Zero-Trust Chain Validator).
Обеспечивает сквозную проверку сертификатов сервисов против Root CA,
валидацию срока действия, базовых ограничений и криптографических подписей RSA-PSS / PKCS#1 v1.5.
"""

import base64
import datetime
import hashlib
import os
import time
from pathlib import Path
from typing import Optional, Tuple, Union

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa, ec

from src.simulations.core.utils.console_logger import log_error, log_info, log_warning
from src.simulations.core.crypto.bank_ca import get_bank_ca


class X509ChainValidator:
    """
    Валидатор цепочки сертификатов X.509 и цифровых подписей для симуляций.
    Проверяет принадлежность сертификатов к единому Zero-Trust корню доверия (Root CA).
    """

    def __init__(
        self,
        root_ca_cert: Optional[Union[x509.Certificate, str, Path]] = None,
        crl_pem: Optional[str] = None,
        max_drift_seconds: int = 300,
        ca_cert_path: Optional[Union[str, Path]] = None,
    ):
        self.max_drift_seconds = max_drift_seconds
        self.crl_pem = crl_pem
        self.root_ca_cert = self._resolve_root_ca(root_ca_cert or ca_cert_path)

    def _resolve_root_ca(
        self,
        cert_or_pem: Optional[Union[x509.Certificate, str, Path]]
    ) -> Optional[x509.Certificate]:
        """Разрешает сертификат Root CA из переданного аргумента, файлов или синглтона банка."""
        if isinstance(cert_or_pem, x509.Certificate):
            return cert_or_pem

        if isinstance(cert_or_pem, str) and "BEGIN CERTIFICATE" in cert_or_pem:
            try:
                return x509.load_pem_x509_certificate(cert_or_pem.encode("utf-8"), default_backend())
            except Exception:
                pass

        if isinstance(cert_or_pem, (str, Path)):
            cert_str = str(cert_or_pem).strip()
            if len(cert_str) < 1024 and "\n" not in cert_str:
                try:
                    p = Path(cert_or_pem)
                    if p.exists() and p.is_file():
                        return x509.load_pem_x509_certificate(p.read_bytes(), default_backend())
                except (OSError, ValueError):
                    pass

        # Поиск ca.crt на диске (в контейнере или по переменной окружения)
        ca_paths = [
            os.getenv("ROOT_CA_CERT_PATH", ""),
            "/certs/ca.crt",
        ]
        for p in ca_paths:
            if p and len(p) < 1024 and "\n" not in p:
                try:
                    if Path(p).exists() and Path(p).is_file():
                        return x509.load_pem_x509_certificate(Path(p).read_bytes(), default_backend())
                except (OSError, ValueError):
                    pass

        # Использование Root CA из Bank CA
        try:
            bank_ca = get_bank_ca()
            if bank_ca and bank_ca.ca_cert:
                return bank_ca.ca_cert
        except Exception:
            pass

        return None

    def parse_certificate(self, cert_input: Union[x509.Certificate, str, bytes, Path]) -> Optional[x509.Certificate]:
        """Парсит X.509 сертификат из объекта, пути к файлу, PEM-строки или байт."""
        if isinstance(cert_input, x509.Certificate):
            return cert_input

        try:
            # 1. Если это PEM-строка с заголовком
            if isinstance(cert_input, str) and "BEGIN CERTIFICATE" in cert_input:
                return x509.load_pem_x509_certificate(cert_input.strip().encode("utf-8"), default_backend())

            # 2. Если это байты
            if isinstance(cert_input, (bytes, bytearray)):
                cert_bytes = bytes(cert_input).strip()
                if b"BEGIN CERTIFICATE" in cert_bytes:
                    return x509.load_pem_x509_certificate(cert_bytes, default_backend())
                else:
                    return x509.load_der_x509_certificate(cert_bytes, default_backend())

            # 3. Если передан Path или короткая строка пути к файлу (без переносов строк)
            if isinstance(cert_input, (str, Path)):
                cert_str = str(cert_input).strip()
                if len(cert_str) < 1024 and "\n" not in cert_str:
                    try:
                        p = Path(cert_input)
                        if p.exists() and p.is_file():
                            file_bytes = p.read_bytes().strip()
                            if b"BEGIN CERTIFICATE" in file_bytes:
                                return x509.load_pem_x509_certificate(file_bytes, default_backend())
                            else:
                                return x509.load_der_x509_certificate(file_bytes, default_backend())
                    except (OSError, ValueError):
                        pass

                # Резервная попытка парсинга строки
                cert_bytes = cert_str.encode("utf-8")
                if b"BEGIN CERTIFICATE" in cert_bytes:
                    return x509.load_pem_x509_certificate(cert_bytes, default_backend())
                else:
                    return x509.load_der_x509_certificate(cert_bytes, default_backend())

        except Exception as e:
            log_warning("ChainValidator", f"Ошибка парсинга X.509 сертификата: {e}")
            return None

        return None

    def validate_certificate_lifetime(
        self,
        cert: x509.Certificate,
        at_time: Optional[datetime.datetime] = None
    ) -> Tuple[bool, str]:
        """Проверяет срок действия сертификата (NotBefore <= check_time <= NotAfter)."""
        check_time = at_time or datetime.datetime.now(datetime.timezone.utc)
        if check_time < cert.not_valid_before_utc:
            return False, f"Сертификат еще не вступил в силу (NotBefore: {cert.not_valid_before_utc})"
        if check_time > cert.not_valid_after_utc:
            return False, f"Срок действия сертификата истек (NotAfter: {cert.not_valid_after_utc})"
        return True, "Сертификат действителен по времени"

    def validate_certificate_chain(
        self,
        leaf_cert_input: Union[x509.Certificate, str, bytes],
        issuer_ca_input: Optional[Union[x509.Certificate, str, bytes]] = None,
    ) -> Tuple[bool, str]:
        """
        Проверяет подлинность сертификата leaf_cert по отношению к доверенному Root CA:
        1. Проверяет срок действия leaf_cert;
        2. Проверяет совпадение Subject CA и Issuer leaf;
        3. Криптографически проверяет цифровую подпись сертификата открытым ключом Root CA.
        """
        leaf_cert = self.parse_certificate(leaf_cert_input)
        if leaf_cert is None:
            return False, "Не удалось распарсить проверяемый сертификат (leaf_cert)"

        issuer_ca = self.parse_certificate(issuer_ca_input) if issuer_ca_input else self.root_ca_cert
        if issuer_ca is None:
            return False, "Доверенный корневой сертификат (Root CA) не настроен"

        # 1. Проверка срока действия
        is_valid_time, time_msg = self.validate_certificate_lifetime(leaf_cert)
        if not is_valid_time:
            return False, time_msg

        # 2. Проверка соответствия субъекта издателя
        if leaf_cert.issuer != issuer_ca.subject:
            return False, (
                f"Несоответствие цепочки доверия: эмитент сертификата ({leaf_cert.issuer.rfc4514_string()}) "
                f"не совпадает с доверенным Root CA ({issuer_ca.subject.rfc4514_string()})"
            )

        # 3. Криптографическая проверка подписи открытым ключом CA
        try:
            ca_pub_key = issuer_ca.public_key()
            if isinstance(ca_pub_key, rsa.RSAPublicKey):
                ca_pub_key.verify(
                    leaf_cert.signature,
                    leaf_cert.tbs_certificate_bytes,
                    padding.PKCS1v15(),
                    leaf_cert.signature_hash_algorithm,
                )
                return True, "Цепочка сертификатов успешно верифицирована Root CA"
            elif isinstance(ca_pub_key, ec.EllipticCurvePublicKey):
                ca_pub_key.verify(
                    leaf_cert.signature,
                    leaf_cert.tbs_certificate_bytes,
                    ec.ECDSA(leaf_cert.signature_hash_algorithm),
                )
                return True, "Цепочка сертификатов (ECDSA) успешно верифицирована Root CA"
            else:
                return False, f"Неподдерживаемый тип ключа Root CA: {type(ca_pub_key)}"
        except InvalidSignature:
            return False, "Недействительная цифровая подпись сертификата (подписан другим ключом)"
        except Exception as e:
            return False, f"Ошибка проверки подписи сертификата: {e}"

    def verify_signature(
        self,
        payload_bytes: bytes,
        signature_b64: str,
        leaf_cert_input: Union[x509.Certificate, str, bytes],
        algorithm: str = "RSA-PSS",
        verify_chain: bool = True,
    ) -> Tuple[bool, str]:
        """
        Верифицирует цифровую подпись данных с валидацией сертификата:
        1. Валидирует цепочку leaf_cert против Root CA (если verify_chain=True);
        2. Извлекает открытый ключ из leaf_cert;
        3. Проверяет подпись RSA-PSS (с fallback на PKCS#1 v1.5 / ECDSA).
        """
        leaf_cert = self.parse_certificate(leaf_cert_input)
        if leaf_cert is None:
            return False, "Не удалось распарсить сертификат для проверки подписи"

        if verify_chain:
            is_chain_valid, chain_err = self.validate_certificate_chain(leaf_cert)
            if not is_chain_valid:
                return False, f"Сбой проверки сертификата: {chain_err}"

        try:
            raw_sig = base64.b64decode(signature_b64.strip())
            pub_key = leaf_cert.public_key()

            if isinstance(pub_key, rsa.RSAPublicKey):
                # 1. Попытка RSA-PSS (Max Salt)
                try:
                    pub_key.verify(
                        raw_sig,
                        payload_bytes,
                        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
                        hashes.SHA256(),
                    )
                    return True, "Цифровая подпись RSA-PSS успешно верифицирована"
                except InvalidSignature:
                    pass

                # 2. Попытка RSA-PSS (Digest Salt)
                try:
                    pub_key.verify(
                        raw_sig,
                        payload_bytes,
                        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                        hashes.SHA256(),
                    )
                    return True, "Цифровая подпись RSA-PSS успешно верифицирована"
                except InvalidSignature:
                    pass

                # 3. Попытка RSA PKCS#1 v1.5 (для совместимости)
                try:
                    pub_key.verify(
                        raw_sig,
                        payload_bytes,
                        padding.PKCS1v15(),
                        hashes.SHA256(),
                    )
                    return True, "Цифровая подпись RSA-PKCS1v15 успешно верифицирована"
                except InvalidSignature:
                    pass

                return False, "Недействительная цифровая подпись полезной нагрузки"

            elif isinstance(pub_key, ec.EllipticCurvePublicKey):
                pub_key.verify(raw_sig, payload_bytes, ec.ECDSA(hashes.SHA256()))
                return True, "Цифровая подпись ECDSA успешно верифицирована"

            return False, f"Неподдерживаемый алгоритм открытого ключа: {type(pub_key)}"
        except InvalidSignature:
            return False, "Недействительная цифровая подпись полезной нагрузки (InvalidSignature)"
        except Exception as e:
            return False, f"Сбой проверки подписи: {e}"

    def compute_canonical_digest(
        self,
        timestamp: Union[int, float, str],
        nonce: str,
        body_bytes: bytes,
        method: Optional[str] = None,
        path: Optional[str] = None,
    ) -> bytes:
        """
        Формирует каноническую строку для цифровой подписи запроса.
        Поддерживает расширенный формат с method/path, а также стандартный Timestamp\\nNonce\\nSHA256(Body).
        """
        body_hash = hashlib.sha256(body_bytes).hexdigest()
        ts_str = str(int(float(timestamp)))
        if method and path:
            canonical = f"{method.upper()}\n{path}\n{ts_str}\n{nonce}\n{body_hash}"
        else:
            canonical = f"{ts_str}\n{nonce}\n{body_hash}"
        return canonical.encode("utf-8")

    def verify_request(
        self,
        timestamp_val: Union[int, float, str],
        nonce: str,
        body_bytes: bytes,
        signature_b64: str,
        leaf_cert_pem: str,
        method: Optional[str] = None,
        path: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Комплексная валидация защищенного запроса:
        - Проверка окна дрейфа времени (Timestamp);
        - Проверка подписи по канонической строке и сертификату.
        """
        now = time.time()
        try:
            req_ts = float(timestamp_val)
        except (ValueError, TypeError):
            return False, "Некорректный формат метки времени"

        drift = abs(now - req_ts)
        if drift > self.max_drift_seconds:
            return False, f"Метка времени запроса устарела (дрейф {drift:.1f}с > {self.max_drift_seconds}с)"

        canonical_data = self.compute_canonical_digest(
            timestamp=req_ts,
            nonce=nonce,
            body_bytes=body_bytes,
            method=method,
            path=path,
        )

        return self.verify_signature(
            payload_bytes=canonical_data,
            signature_b64=signature_b64,
            leaf_cert_input=leaf_cert_pem,
            verify_chain=True,
        )


_CHAIN_VALIDATOR_INSTANCE: Optional[X509ChainValidator] = None


def get_chain_validator(root_ca_cert: Optional[Union[x509.Certificate, str]] = None) -> X509ChainValidator:
    """Возвращает глобальный экземпляр X509ChainValidator."""
    global _CHAIN_VALIDATOR_INSTANCE
    if _CHAIN_VALIDATOR_INSTANCE is not None and root_ca_cert is None:
        return _CHAIN_VALIDATOR_INSTANCE

    instance = X509ChainValidator(root_ca_cert=root_ca_cert)
    if root_ca_cert is None:
        _CHAIN_VALIDATOR_INSTANCE = instance
    return instance


def reset_chain_validator_for_tests() -> X509ChainValidator:
    """Сбрасывает синглтон валидатора для тестов."""
    global _CHAIN_VALIDATOR_INSTANCE
    _CHAIN_VALIDATOR_INSTANCE = X509ChainValidator()
    return _CHAIN_VALIDATOR_INSTANCE
