"""
Криптографический валидатор сертификатов и ключей Zero-Trust PKI.
Обеспечивает строгую проверку цепочки доверия X.509, срока действия,
интеграцию с системным хранилищем сертификатов Windows (certmgr.msc / crypt32.dll)
и отказ от самоподписанных сертификатов.
"""

import sys
import os
import datetime
import tempfile
from typing import Optional, Any, Tuple
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives.asymmetric import rsa, ec, padding
from cryptography.hazmat.primitives import hashes, serialization


class CertificateMissingError(RuntimeError):
    """Выбрасывается, когда обязательный клиентский или корневой сертификат не найден."""
    pass


class WindowsCertificateStoreError(CertificateMissingError):
    """
    Выбрасывается, когда требуемый сертификат (Root CA или клиентский сертификат)
    не обнаружен в системном хранилище сертификатов Windows (certmgr.msc).
    Предоставляет пользователю исчерпывающее руководство по установке сертификата.
    """
    pass


class CertificateValidationError(RuntimeError):
    """Выбрасывается, когда сертификат не прошел криптографическую проверку (истек, самоподписан, не подписан Root CA)."""
    pass


class KeyMissingError(RuntimeError):
    """Выбрасывается, когда обязательный закрытый ключ не найден на диске."""
    pass


def get_windows_cert_store_help_message(missing_component: str = "all") -> str:
    """
    Генерирует понятное сообщение с инструкцией по установке сертификатов через certmgr.msc.
    """
    lines = [
        "================================================================================",
        "ОШИБКА ZERO-TRUST БЕЗОПАСНОСТИ: СЕРТИФИКАТЫ НЕ НАЙДЕНЫ В СИСТЕМНОМ ХРАНИЛИЩЕ WINDOWS",
        "================================================================================",
    ]
    if missing_component in ("root_ca", "all"):
        lines.extend([
            "[-] Сертификат не найден: доверенный корневой сертификат (Root CA 'ca.crt').",
            "    Хранилище: 'Доверенные корневые центры сертификации' (Trusted Root Certification Authorities / ROOT).",
            "    Инструкция по установке:",
            "      1. Нажмите сочетание клавиш Win + R, введите 'certmgr.msc' и нажмите Enter.",
            "      2. В левой панели раскройте: 'Доверенные корневые центры сертификации' -> 'Сертификаты'.",
            "      3. Нажмите правой кнопкой мыши по папке 'Сертификаты' -> 'Все задачи' -> 'Импорт...'.",
            "      4. Выберите файл корневого сертификата ca.crt и завершите работу мастера импорта.",
            "",
        ])
    if missing_component in ("client_cert", "all"):
        lines.extend([
            "[-] Сертификат не найден: клиентский сертификат администратора ('admin_client.crt').",
            "    Хранилище: 'Личные' (Personal / MY).",
            "    Инструкция по установке:",
            "      1. Нажмите сочетание клавиш Win + R, введите 'certmgr.msc' и нажмите Enter.",
            "      2. В левой панели раскройте: 'Личные' -> 'Сертификаты'.",
            "      3. Нажмите правой кнопкой мыши по папке 'Сертификаты' -> 'Все задачи' -> 'Импорт...'.",
            "      4. Выберите файл клиентского сертификата admin_client.crt (или контейнер .pfx).",
            "",
        ])
    lines.extend([
        "Альтернатива (для файлового развертывания):",
        "  - Передайте путь к ca.crt через переменную окружения CA_CERT_PATH или аргумент ca_cert_path.",
        "  - Передайте путь к admin_client.crt через переменную окружения ADMIN_CLIENT_CERT_PATH или аргумент cert_path.",
        "================================================================================"
    ])
    return "\n".join(lines)


def find_cert_in_windows_store(
    store_name: str = "MY",
    expected_public_key: Optional[Any] = None,
    subject_cn: Optional[str] = None,
    issuer_cn: Optional[str] = None,
    is_ca: Optional[bool] = None,
) -> Optional[Tuple[str, x509.Certificate]]:
    """
    Выполняет поиск действующего X.509 сертификата в системных хранилищах Windows
    ('MY' - Личные, 'ROOT' - Доверенные корневые центры сертификации) через CryptoAPI (crypt32.dll).
    Проверяет CurrentUser, затем LocalMachine.
    """
    if sys.platform != "win32":
        return None

    try:
        import ctypes
        from ctypes import wintypes

        crypt32 = getattr(ctypes.windll, "crypt32", None)
        if not crypt32:
            return None

        crypt32.CertOpenStore.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.LPCWSTR
        ]
        crypt32.CertOpenStore.restype = ctypes.c_void_p

        crypt32.CertEnumCertificatesInStore.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        crypt32.CertEnumCertificatesInStore.restype = ctypes.c_void_p

        crypt32.CertCloseStore.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        crypt32.CertCloseStore.restype = wintypes.BOOL

        crypt32.CertFreeCertificateContext.argtypes = [ctypes.c_void_p]
        crypt32.CertFreeCertificateContext.restype = wintypes.BOOL

        CERT_STORE_PROV_SYSTEM_W = 10
        CERT_SYSTEM_STORE_CURRENT_USER = 0x00010000
        CERT_SYSTEM_STORE_LOCAL_MACHINE = 0x00020000
        CERT_STORE_READONLY_FLAG = 0x00008000

        class CERT_CONTEXT(ctypes.Structure):
            _fields_ = [
                ("dwCertEncodingType", wintypes.DWORD),
                ("pbCertEncoded", ctypes.c_void_p),
                ("cbCertEncoded", wintypes.DWORD),
                ("pCertInfo", ctypes.c_void_p),
                ("hCertStore", ctypes.c_void_p),
            ]

        # Нормализация ожидаемого публичного ключа в DER байты
        exp_der: Optional[bytes] = None
        if expected_public_key is not None:
            if isinstance(expected_public_key, (rsa.RSAPublicKey, ec.EllipticCurvePublicKey)):
                exp_der = expected_public_key.public_bytes(
                    encoding=serialization.Encoding.DER,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo
                )
            elif isinstance(expected_public_key, str) and "-----BEGIN" in expected_public_key:
                exp_key = serialization.load_pem_public_key(expected_public_key.encode("utf-8"))
                exp_der = exp_key.public_bytes(
                    encoding=serialization.Encoding.DER,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo
                )
            elif isinstance(expected_public_key, bytes) and b"-----BEGIN" in expected_public_key:
                exp_key = serialization.load_pem_public_key(expected_public_key)
                exp_der = exp_key.public_bytes(
                    encoding=serialization.Encoding.DER,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo
                )
            elif isinstance(expected_public_key, bytes):
                exp_der = expected_public_key

        now = datetime.datetime.now(datetime.timezone.utc)
        store_locations = [
            CERT_SYSTEM_STORE_CURRENT_USER | CERT_STORE_READONLY_FLAG,
            CERT_SYSTEM_STORE_LOCAL_MACHINE | CERT_STORE_READONLY_FLAG,
        ]

        for loc_flags in store_locations:
            h_store = crypt32.CertOpenStore(
                CERT_STORE_PROV_SYSTEM_W,
                0,
                None,
                loc_flags,
                store_name
            )
            if not h_store:
                continue

            p_cert = None
            try:
                while True:
                    p_cert = crypt32.CertEnumCertificatesInStore(h_store, p_cert)
                    if not p_cert:
                        break

                    ctx = CERT_CONTEXT.from_address(p_cert)
                    raw_der = ctypes.string_at(ctx.pbCertEncoded, ctx.cbCertEncoded)
                    try:
                        cert_obj = x509.load_der_x509_certificate(raw_der)
                    except Exception:
                        continue

                    # Проверка срока действия
                    if cert_obj.not_valid_after_utc < now or cert_obj.not_valid_before_utc > now:
                        continue

                    # Проверка флага CA
                    if is_ca is not None:
                        cert_is_ca = False
                        try:
                            bc = cert_obj.extensions.get_extension_for_oid(x509.ExtensionOID.BASIC_CONSTRAINTS).value
                            cert_is_ca = bool(bc.ca)
                        except Exception:
                            cert_is_ca = False
                        if cert_is_ca != is_ca:
                            continue

                    # Проверка открытого ключа
                    if exp_der is not None:
                        cert_pub_der = cert_obj.public_key().public_bytes(
                            encoding=serialization.Encoding.DER,
                            format=serialization.PublicFormat.SubjectPublicKeyInfo
                        )
                        if cert_pub_der != exp_der:
                            continue

                    # Проверка CN субъекта
                    if subject_cn is not None:
                        cns = [a.value for a in cert_obj.subject.get_attributes_for_oid(NameOID.COMMON_NAME)]
                        if not any(subject_cn.lower() in str(cn).lower() for cn in cns):
                            continue

                    # Проверка CN издателя
                    if issuer_cn is not None:
                        icns = [a.value for a in cert_obj.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)]
                        if not any(issuer_cn.lower() in str(icn).lower() for icn in icns):
                            continue

                    # Подходящий сертификат найден!
                    pem_data = cert_obj.public_bytes(serialization.Encoding.PEM).decode("utf-8")
                    crypt32.CertFreeCertificateContext(p_cert)
                    p_cert = None
                    return pem_data, cert_obj
            finally:
                if p_cert:
                    crypt32.CertFreeCertificateContext(p_cert)
                crypt32.CertCloseStore(h_store, 0)

    except Exception:
        pass

    return None


def find_root_ca_in_windows_store(
    expected_issuer_cn: Optional[str] = None
) -> Optional[Tuple[str, x509.Certificate]]:
    """
    Ищет доверенный корневой сертификат Root CA в системном хранилище Windows 'ROOT'.
    """
    target_cn = expected_issuer_cn or "Financial AI Agent Root CA"
    result = find_cert_in_windows_store(
        store_name="ROOT",
        subject_cn=target_cn,
        is_ca=True
    )
    if result:
        return result

    if not expected_issuer_cn:
        result = find_cert_in_windows_store(
            store_name="ROOT",
            subject_cn="Root CA",
            is_ca=True
        )
        if result:
            return result
    return None


def find_client_cert_in_windows_store(
    expected_public_key: Optional[Any] = None,
    subject_cn: Optional[str] = None
) -> Optional[Tuple[str, x509.Certificate]]:
    """
    Ищет клиентский сертификат администратора в системном хранилище Windows 'MY' (Личные).
    """
    if expected_public_key is not None:
        result = find_cert_in_windows_store(
            store_name="MY",
            expected_public_key=expected_public_key,
            is_ca=False
        )
        if result:
            return result

    candidates = [subject_cn] if subject_cn else [
        "superadmin-workstation",
        "admin_client",
        "Financial AI Agent Workstation",
        "admin_operator"
    ]
    for cand in candidates:
        if cand:
            result = find_cert_in_windows_store(
                store_name="MY",
                subject_cn=cand,
                is_ca=False
            )
            if result:
                return result

    return None


def export_windows_cert_to_cache(pem_data: str, filename: str) -> str:
    """
    Сохраняет PEM-сертификат в защищенную локальную директорию кэша (%LOCALAPPDATA%/FinancialAgent/certs/).
    Возвращает абсолютный путь к файлу сертификата на диске для requests / caddy.
    """
    if sys.platform == "win32":
        base_dir = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or tempfile.gettempdir()
    else:
        base_dir = os.path.expanduser("~/.financial_agent")

    cache_dir = os.path.join(base_dir, "FinancialAgent", "certs")
    os.makedirs(cache_dir, exist_ok=True)

    target_path = os.path.join(cache_dir, filename)
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(pem_data.strip() + "\n")

    if hasattr(os, "chmod") and sys.platform != "win32":
        try:
            os.chmod(target_path, 0o600)
        except Exception:
            pass

    return target_path


def find_certificate_file(
    custom_path: Optional[str] = None,
    env_var: str = "ADMIN_CLIENT_CERT_PATH",
    default_filenames: tuple = ("admin_client.crt",)
) -> Optional[str]:
    """
    Ищет файл сертификата по переданному пути, переменным окружения и стандартным путям системы.
    """
    if custom_path and os.path.exists(custom_path):
        return custom_path

    env_val = os.getenv(env_var)
    if env_val and os.path.exists(env_val):
        return env_val

    candidates = []
    for fname in default_filenames:
        candidates.extend([
            f"./certs/{fname}",
            f"/certs/{fname}",
            f"certs/{fname}",
        ])
        if sys.platform == "win32":
            appdata = os.getenv("APPDATA")
            if appdata:
                candidates.append(os.path.join(appdata, "FinancialAgent", "certs", fname))
            localappdata = os.getenv("LOCALAPPDATA")
            if localappdata:
                candidates.append(os.path.join(localappdata, "FinancialAgent", "certs", fname))
        else:
            candidates.append(os.path.expanduser(f"~/.financial_agent/certs/{fname}"))

    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def resolve_and_validate_client_certificate(
    cert_path: Optional[str] = None,
    cert_pem: Optional[str] = None,
    ca_cert_path: Optional[str] = None,
    ca_cert_pem: Optional[str] = None,
    expected_public_key: Optional[Any] = None,
    require_ca: bool = True
) -> Tuple[str, x509.Certificate]:
    """
    Разрешает и строго валидирует клиентский X.509 сертификат:
    1. Поиск клиентского сертификата: cert_pem -> cert_path / ADMIN_CLIENT_CERT_PATH ->
       стандартные пути на диске -> Windows Certificate Store ('MY').
       Если не найден — WindowsCertificateStoreError.
    2. Проверка срока действия (NotBefore <= now <= NotAfter). Если истек — CertificateValidationError.
    3. Запрет самоподписанных сертификатов (Subject == Issuer). Если самоподписан — CertificateValidationError.
    4. Поиск и валидация доверенной цепочки Root CA: ca_cert_pem -> ca_cert_path / CA_CERT_PATH ->
       стандартные пути на диске -> Windows Certificate Store ('ROOT').
       Если Root CA не найден и require_ca=True — WindowsCertificateStoreError.
       Если найден, но подпись неверна — CertificateValidationError.
    5. Если передан expected_public_key, проверка совпадения с открытым ключом сертификата.

    Returns:
        Tuple[str, x509.Certificate]: PEM строка и объект распарсенного сертификата.
    """
    # 1. Поиск клиентского сертификата
    pem_data = ""
    if cert_pem and cert_pem.strip():
        pem_data = cert_pem.strip()
    else:
        found_path = find_certificate_file(
            custom_path=cert_path,
            env_var="ADMIN_CLIENT_CERT_PATH",
            default_filenames=("admin_client.crt",)
        )
        if found_path:
            try:
                with open(found_path, "r", encoding="utf-8") as f:
                    pem_data = f.read().strip()
            except Exception as e:
                raise CertificateMissingError(f"Ошибка чтения файла сертификата {found_path}: {e}")
        else:
            win_cert = find_client_cert_in_windows_store(expected_public_key=expected_public_key)
            if win_cert:
                pem_data, _ = win_cert
            else:
                raise WindowsCertificateStoreError(get_windows_cert_store_help_message("client_cert"))

    if "-----BEGIN CERTIFICATE-----" not in pem_data:
        raise CertificateValidationError("Содержимое клиентского сертификата не является валидным PEM X.509")

    try:
        cert = x509.load_pem_x509_certificate(pem_data.encode("utf-8"))
    except Exception as e:
        raise CertificateValidationError(f"Ошибка парсинга X.509 сертификата: {e}")

    # 2. Проверка срока действия
    now = datetime.datetime.now(datetime.timezone.utc)
    if now < cert.not_valid_before_utc:
        raise CertificateValidationError(
            f"Срок действия клиентского сертификата еще не наступил (действителен с: {cert.not_valid_before_utc})"
        )
    if now > cert.not_valid_after_utc:
        raise CertificateValidationError(
            f"Срок действия клиентского сертификата истек (действителен до: {cert.not_valid_after_utc})"
        )

    # 3. Строгий запрет самоподписанных сертификатов
    if cert.issuer == cert.subject:
        raise CertificateValidationError(
            f"Клиентский сертификат является самоподписанным (Subject == Issuer: '{cert.subject}'). "
            "В боевой архитектуре Zero-Trust самоподписанные сертификаты строго запрещены: "
            "сертификат обязан быть подписан Root CA."
        )

    # 4. Проверка подписи Root CA
    ca_pem_data = ""
    if ca_cert_pem and ca_cert_pem.strip():
        ca_pem_data = ca_cert_pem.strip()
    else:
        found_ca = find_certificate_file(
            custom_path=ca_cert_path,
            env_var="CA_CERT_PATH",
            default_filenames=("ca.crt",)
        )
        if found_ca:
            try:
                with open(found_ca, "r", encoding="utf-8") as f:
                    ca_pem_data = f.read().strip()
            except Exception:
                pass
        else:
            issuer_cns = [attr.value for attr in cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)]
            target_issuer = issuer_cns[0] if issuer_cns else None
            win_ca = find_root_ca_in_windows_store(expected_issuer_cn=target_issuer)
            if win_ca:
                ca_pem_data, _ = win_ca

    if ca_pem_data and "-----BEGIN CERTIFICATE-----" in ca_pem_data:
        try:
            ca_cert = x509.load_pem_x509_certificate(ca_pem_data.encode("utf-8"))
            ca_pub = ca_cert.public_key()
            if isinstance(ca_pub, rsa.RSAPublicKey):
                ca_pub.verify(
                    cert.signature,
                    cert.tbs_certificate_bytes,
                    padding.PKCS1v15(),
                    cert.signature_hash_algorithm
                )
            elif hasattr(ca_pub, "verify"):
                ca_pub.verify(
                    cert.signature,
                    cert.tbs_certificate_bytes,
                    cert.signature_hash_algorithm
                )
        except Exception as e:
            raise CertificateValidationError(
                f"Клиентский сертификат не прошел верификацию подписи доверенным Root CA ({ca_cert.subject}): {e}"
            )
    elif require_ca:
        raise WindowsCertificateStoreError(get_windows_cert_store_help_message("root_ca"))

    # 5. Проверка соответствия открытого ключа
    if expected_public_key is not None:
        cert_pub_der = cert.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )
        if isinstance(expected_public_key, (rsa.RSAPublicKey, ec.EllipticCurvePublicKey)):
            exp_pub_der = expected_public_key.public_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PublicFormat.SubjectPublicKeyInfo
            )
        elif isinstance(expected_public_key, str) and "-----BEGIN" in expected_public_key:
            exp_key = serialization.load_pem_public_key(expected_public_key.encode("utf-8"))
            exp_pub_der = exp_key.public_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PublicFormat.SubjectPublicKeyInfo
            )
        elif isinstance(expected_public_key, bytes) and b"-----BEGIN" in expected_public_key:
            exp_key = serialization.load_pem_public_key(expected_public_key)
            exp_pub_der = exp_key.public_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PublicFormat.SubjectPublicKeyInfo
            )
        elif isinstance(expected_public_key, bytes):
            exp_pub_der = expected_public_key
        else:
            exp_pub_der = None

        if exp_pub_der is not None and cert_pub_der != exp_pub_der:
            raise CertificateValidationError(
                "Публичный ключ клиентского сертификата не совпадает с публичным ключом рабочего закрытого/аппаратного ключа"
            )

    return pem_data, cert


def create_test_ca_and_client_cert(
    cn: str = "superadmin-workstation",
    role: str = "admin_operator",
    client_private_key: Optional[rsa.RSAPrivateKey] = None,
    validity_days: int = 365
) -> Tuple[str, str, rsa.RSAPrivateKey]:
    """
    Вспомогательная функция для генерации легитимного Root CA и подписанного им клиентского сертификата.
    Используется в тестировании для создания честной доверенной цепочки без самоподписанных клиентских сертификатов.

    Returns:
        Tuple[str, str, rsa.RSAPrivateKey]: (ca_cert_pem, client_cert_pem, client_private_key)
    """
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Financial AI Agent Root Authority"),
        x509.NameAttribute(NameOID.COMMON_NAME, "Financial AI Agent Root CA"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    cli_key = client_private_key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cli_name = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Financial AI Agent Workstation"),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, role),
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
    ])
    cli_cert = (
        x509.CertificateBuilder()
        .subject_name(cli_name)
        .issuer_name(ca_name)
        .public_key(cli_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=validity_days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    cli_pem = cli_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    return ca_pem, cli_pem, cli_key
