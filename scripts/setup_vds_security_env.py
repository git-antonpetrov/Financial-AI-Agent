#!/usr/bin/env python3
"""
Скрипт первоначальной инициализации обязательных переменных окружения безопасности на сервере (VDS):
- Генерация 2048-битной RSA-пары для асимметричной подписи JWT (RS256);
- Генерация 256-битного мастер-ключа шифрования Data at Rest (DATA_ENCRYPTION_KEY);
- Генерация секрета 2FA TOTP (ADMIN_TOTP_SECRET);
- Генерация криптостойких паролей для БД, Redis, MinIO и ChromaDB (при отсутствии или небезопасных значениях);
- Настройка параметров сквозного Zero-Trust шифрования (POSTGRES_SSLMODE=require, REDIS_SSL=true, MINIO_SECURE=true);
- Настройка параметров Caddy и mTLS.

Не изменяет исходники admin-server и security.py: список субъектов mTLS
записывает только в файл окружения.
"""

import base64
import os
import secrets
import subprocess
import sys
from pathlib import Path

_REPEATED_HEX_BLOCK = "0123456789abcdef"
_WEAK_TOTP_BLOCK = "JBSWY3DPEHPK3PXP"
_SUPERADMIN_CLIENT_CN = "superadmin-workstation"
_KNOWN_KMS_PLACEHOLDER = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
_EXTRA_WEAK_SECRETS = {
    "PROXY_INTERNAL_SECRET": "financial-core-proxy-token-2026",
}

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def generate_rsa_keypair(priv_path: str | None = None, pub_path: str | None = None) -> tuple[str, str]:
    """
    Генерирует пару RSA-2048 в оперативной памяти через библиотеку cryptography.
    Опционально сохраняет в файлы, если пути переданы явно (для совместимости).
    """
    try:
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        priv_bytes = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pub_bytes = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        if priv_path and pub_path:
            os.makedirs(os.path.dirname(os.path.abspath(priv_path)), exist_ok=True)
            os.makedirs(os.path.dirname(os.path.abspath(pub_path)), exist_ok=True)
            with open(priv_path, "wb") as f:
                f.write(priv_bytes)
            with open(pub_path, "wb") as f:
                f.write(pub_bytes)
            try:
                os.chmod(priv_path, 0o600)
                os.chmod(pub_path, 0o644)
            except Exception:
                pass

        priv_pem = priv_bytes.decode("utf-8").strip().replace("\n", "\\n")
        pub_pem = pub_bytes.decode("utf-8").strip().replace("\n", "\\n")
        return priv_pem, pub_pem
    except Exception:
        if priv_path and pub_path:
            os.makedirs(os.path.dirname(os.path.abspath(priv_path)), exist_ok=True)
            os.makedirs(os.path.dirname(os.path.abspath(pub_path)), exist_ok=True)
            subprocess.run(
                ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", priv_path],
                check=True,
            )
            subprocess.run(
                ["openssl", "rsa", "-in", priv_path, "-pubout", "-out", pub_path],
                check=True,
            )
            with open(priv_path, "r", encoding="utf-8") as f:
                priv_pem = f.read().strip().replace("\n", "\\n")
            with open(pub_path, "r", encoding="utf-8") as f:
                pub_pem = f.read().strip().replace("\n", "\\n")
            return priv_pem, pub_pem
        raise RuntimeError("Ошибка генерации RSA ключей подписи JWT в памяти.")


def is_repeated_token(value: str, block: str) -> bool:
    """Сообщает, что значение пустое или состоит только из повторов известного блока.

    Аргументы:
        value: Текущее значение переменной окружения.
        block: Известный слабый фрагмент из шаблона.

    Возвращает:
        True, если значение нужно заменить. Случайная строка той же длины остаётся.
    """
    if not value:
        return True
    if not block or len(value) % len(block) != 0:
        return False
    return value == block * (len(value) // len(block))


def is_known_weak_kms(value: str) -> bool:
    """Сообщает, что ключ MinIO KMS пустой или совпадает с известным плейсхолдером.

    Длина хвоста 44 или 64 сама по себе не считается слабостью: столько же
    символов у нормального Base64 и hex.

    Аргументы:
        value: Текущее значение MINIO_KMS_SECRET_KEY.

    Возвращает:
        True, если ключ нужно сгенерировать заново.
    """
    raw = value.strip().strip('"').strip("'")
    if not raw or _KNOWN_KMS_PLACEHOLDER in raw:
        return True
    tail = raw.split(":")[-1]
    return is_repeated_token(tail, _REPEATED_HEX_BLOCK)


def common_name_from_certificate(path: Path) -> str:
    """Читает CN из PEM-сертификата.

    Аргументы:
        path: Путь к файлу сертификата.

    Возвращает:
        CN либо пустую строку, если файл отсутствует или не читается.
    """
    if not path.is_file():
        return ""
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID

        certificate = x509.load_pem_x509_certificate(path.read_bytes())
        attributes = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    except Exception:
        return ""
    if not attributes:
        return ""
    return str(attributes[0].value).replace("\n", "").replace("\r", "").strip()


def issued_superadmin_common_name(project_dir: str) -> str:
    """Возвращает CN выпущенного клиента superadmin-workstation.

    Аргументы:
        project_dir: Корень проекта, где может лежать уже выпущенный сертификат.

    Возвращает:
        CN из admin_client.crt, если файл читается. Иначе CN, который
        bootstrap PKI назначает рабочей станции супер-администратора.
    """
    root = Path(project_dir)
    candidates = (
        root / "certs" / "admin_client.crt",
        root / "certs" / "admin_client_certs" / "admin_client.crt",
        root / "certs" / "client.crt",
    )
    for path in candidates:
        common_name = common_name_from_certificate(path)
        if common_name:
            return common_name
    return _SUPERADMIN_CLIENT_CN


def store_initial_admin_password(project_dir: str, password: str, explicit_path: str | None) -> str:
    """Записывает первичный пароль администратора в файл и не возвращает его в журнал.

    Аргументы:
        project_dir: Корень проекта, если явный путь не задан.
        password: Пароль, который только что сгенерирован.
        explicit_path: Куда записать пароль. Пустое значение означает `.secrets` в проекте.

    Возвращает:
        Путь к файлу с паролем.

    Побочные эффекты:
        Создаёт каталог с режимом 0700 и файл с режимом 0600, если ОС позволяет.
    """
    if explicit_path:
        path = Path(explicit_path)
    else:
        path = Path(project_dir) / ".secrets" / "initial_admin_password"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    path.write_text(password + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return str(path)


def setup_vds(project_dir: str | None = None, initial_password_file: str | None = None) -> dict[str, str]:
    """Заполняет .env криптостойкими секретами и не трогает исходники admin-server.

    Заменяет известные плейсхолдеры из шаблона и оставляет уже случайные значения.
    Пустой ALLOWED_MTLS_SUBJECTS получает CN клиента superadmin-workstation.
    Сравнение субъектов в security.py не меняет. Пароль администратора пишет
    в файл и не печатает.

    Аргументы:
        project_dir: Корень проекта. Пустое значение означает каталог над scripts.
        initial_password_file: Файл первичного пароля. Пустое значение означает
            `.secrets/initial_admin_password` внутри проекта.

    Возвращает:
        Словарь с описанием значений, которые функция сгенерировала в этот запуск.
        Сам пароль в словарь не входит.

    Побочные эффекты:
        Создаёт или перезаписывает .env и выставляет права 0600, если ОС позволяет.
        При смене пароля администратора создаёт файл с этим паролем.
    """
    if project_dir is None:
        project_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    else:
        project_dir = os.path.abspath(project_dir)

    env_path = os.path.join(project_dir, ".env")
    example_path = os.path.join(project_dir, ".env.example")
    data_dir = os.path.join(project_dir, "data")
    os.makedirs(data_dir, exist_ok=True)

    env_lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            env_lines = f.readlines()
    elif os.path.exists(example_path):
        with open(example_path, "r", encoding="utf-8") as f:
            env_lines = f.readlines()

    # Разбирает существующие ключи
    env_dict: dict[str, str] = {}
    for line in env_lines:
        line_s = line.strip()
        if line_s and not line_s.startswith("#") and "=" in line_s:
            k, v = line_s.split("=", 1)
            env_dict[k.strip()] = v.strip().strip('"').strip("'")

    generated_info: dict[str, str] = {}

    # 1. Генерация RSA пары для асимметричной подписи JWT в оперативной памяти (RS256)
    cur_jwt_priv = env_dict.get("JWT_PRIVATE_KEY", "").strip()
    cur_jwt_pub = env_dict.get("JWT_PUBLIC_KEY", "").strip()
    if (
        not cur_jwt_priv
        or not cur_jwt_pub
        or "change_this_rsa_private_key" in cur_jwt_priv
        or "BEGIN RSA PRIVATE KEY" in cur_jwt_priv
    ):
        priv_pem, pub_pem = generate_rsa_keypair()
        generated_info["JWT_KEY_ALGORITHM"] = "RS256 (2048-bit in-memory)"
    else:
        priv_pem = cur_jwt_priv.strip('"')
        pub_pem = cur_jwt_pub.strip('"')

    # 2. Мастер-ключ шифрования Data at Rest (AES-256-GCM)
    cur_enc_key = env_dict.get("DATA_ENCRYPTION_KEY", "")
    if is_repeated_token(cur_enc_key, _REPEATED_HEX_BLOCK):
        env_dict["DATA_ENCRYPTION_KEY"] = secrets.token_hex(32)
        generated_info["DATA_ENCRYPTION_KEY"] = "Сгенерирован новый 256-битный ключ AES-GCM"

    # 3. TOTP секрет для 2FA (RFC 6238)
    cur_totp = env_dict.get("ADMIN_TOTP_SECRET", "")
    if is_repeated_token(cur_totp, _WEAK_TOTP_BLOCK):
        env_dict["ADMIN_TOTP_SECRET"] = base64.b32encode(secrets.token_bytes(20)).decode("utf-8").rstrip("=")
        generated_info["ADMIN_TOTP_SECRET"] = "Сгенерирован новый Base32 TOTP секрет"

    # 4. Пароли инфраструктурных сервисов
    placeholders = {
        "POSTGRES_PASSWORD": ("change_this_strong_password_in_production", lambda: secrets.token_urlsafe(24)),
        "BANK_DB_PASSWORD": ("change_this_bank_db_password", lambda: secrets.token_hex(24)),
        "INVEST_DB_PASSWORD": ("change_this_invest_db_password", lambda: secrets.token_hex(24)),
        "DIGITAL_DB_PASSWORD": ("change_this_digital_db_password", lambda: secrets.token_hex(24)),
        "REDIS_PASSWORD": ("change_this_redis_password", lambda: secrets.token_urlsafe(24)),
        "PGADMIN_DEFAULT_PASSWORD": ("change_this_pgadmin_password", lambda: secrets.token_urlsafe(24)),
        "MINIO_ACCESS_KEY": ("minio_access_key_placeholder", lambda: secrets.token_hex(12)),
        "MINIO_SECRET_KEY": ("minio_secret_key_placeholder", lambda: secrets.token_urlsafe(32)),
        "CHROMA_AUTH_TOKEN": ("chroma_token_secret_placeholder", lambda: secrets.token_urlsafe(24)),
        "AGENT_DIGITAL_BOOTSTRAP_TOKEN": ("bootstrap_token_digital_agent_secret", lambda: secrets.token_urlsafe(32)),
        "AGENT_BANK_BOOTSTRAP_TOKEN": ("bootstrap_token_bank_agent_secret", lambda: secrets.token_urlsafe(32)),
        "AGENT_INVEST_BOOTSTRAP_TOKEN": ("bootstrap_token_invest_agent_secret", lambda: secrets.token_urlsafe(32)),
        "AGENT_MAIN_BOOTSTRAP_TOKEN": ("bootstrap_token_main_agent_secret", lambda: secrets.token_urlsafe(32)),
        "ORACLE_BOOTSTRAP_TOKEN": ("bootstrap_token_oracle_secret", lambda: secrets.token_urlsafe(32)),
        "AGENT_ORACLE_BOOTSTRAP_TOKEN": ("bootstrap_token_oracle_agent_secret", lambda: secrets.token_urlsafe(32)),
        "PROXY_INTERNAL_SECRET": ("change_this_proxy_internal_secret", lambda: secrets.token_urlsafe(32)),
    }

    for key, (placeholder_val, gen_fn) in placeholders.items():
        cur_val = env_dict.get(key, "")
        if not cur_val or cur_val == placeholder_val or cur_val == _EXTRA_WEAK_SECRETS.get(key):
            new_val = gen_fn()
            env_dict[key] = new_val
            generated_info[key] = "Сгенерирован надежный случайный пароль/токен"

    # 5. MinIO KMS ключ (32 байта в кодировке Base64)
    cur_kms = env_dict.get("MINIO_KMS_SECRET_KEY", "")
    if is_known_weak_kms(cur_kms):
        kms_b64 = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
        env_dict["MINIO_KMS_SECRET_KEY"] = f"financial-kms-key:{kms_b64}"
        generated_info["MINIO_KMS_SECRET_KEY"] = "Сгенерирован ключ MinIO KMS SSE-S3 (32 байта Base64)"

    # Записывает CN в окружение и не меняет сравнение субъектов в security.py,
    # потому что этот модуль правит другой проход.
    if not env_dict.get("ALLOWED_MTLS_SUBJECTS", "").strip():
        env_dict["ALLOWED_MTLS_SUBJECTS"] = issued_superadmin_common_name(project_dir)
        generated_info["ALLOWED_MTLS_SUBJECTS"] = "Заполнен CN клиента superadmin-workstation"

    # 6. Хэш пароля администратора
    cur_admin_hash = env_dict.get("ADMIN_PASSWORD_HASH", "")
    initial_admin_password = None
    if not cur_admin_hash or "eXampLeHaShValueForAdMinPassWordPlaceholder" in cur_admin_hash:
        try:
            import bcrypt
            initial_admin_password = f"Admin_{secrets.token_urlsafe(12)}!"
            env_dict["ADMIN_PASSWORD_HASH"] = bcrypt.hashpw(
                initial_admin_password.encode("utf-8"), bcrypt.gensalt()
            ).decode("utf-8")
        except Exception as error:
            raise RuntimeError(
                "Не удалось вычислить хэш пароля администратора, потому что пакет bcrypt недоступен."
            ) from error

    # 7. Парольная фраза Root CA (AES-256)
    cur_ca_pass = env_dict.get("ROOT_CA_PASSPHRASE", "")
    if not cur_ca_pass or cur_ca_pass.startswith("change_this_strong_root_ca_passphrase"):
        env_dict["ROOT_CA_PASSPHRASE"] = secrets.token_urlsafe(32)
        generated_info["ROOT_CA_PASSPHRASE"] = "Сгенерирована криптостойкая парольная фраза Root CA"

    # 8. Принудительные параметры Zero-Trust In-Transit TLS & RSA JWT
    env_dict["JWT_PRIVATE_KEY"] = f'"{priv_pem}"' if not priv_pem.startswith('"') else priv_pem
    env_dict["JWT_PUBLIC_KEY"] = f'"{pub_pem}"' if not pub_pem.startswith('"') else pub_pem
    env_dict["POSTGRES_SSLMODE"] = "verify-full"
    env_dict["REDIS_SSL"] = "true"
    env_dict["MINIO_SECURE"] = "true"
    env_dict["MINIO_SSE_ENABLED"] = "true"
    env_dict["MINIO_SSE_TYPE"] = env_dict.get("MINIO_SSE_TYPE", "s3")
    env_dict["CHROMA_ENVELOPE_ENCRYPTION_ENABLED"] = "true"
    env_dict["CADDY_CLIENT_AUTH_MODE"] = env_dict.get("CADDY_CLIENT_AUTH_MODE", "require_and_verify")
    env_dict["REQUIRE_MTLS"] = env_dict.get("REQUIRE_MTLS", "true")
    env_dict["REQUIRE_2FA"] = env_dict.get("REQUIRE_2FA", "true")
    env_dict["STRICT_SECURITY"] = env_dict.get("STRICT_SECURITY", "true")
    env_dict["ADMIN_DOMAIN"] = env_dict.get("ADMIN_DOMAIN", "admin.fin-ai-agent.ru")
    env_dict["ADMIN_LOCAL_DOMAIN"] = env_dict.get("ADMIN_LOCAL_DOMAIN", "admin.fin-ai-agent.local")
    env_dict["FORCE_REGENERATE"] = env_dict.get("FORCE_REGENERATE", "false")

    # Перезаписывает или обновляет .env
    written_keys = set()
    output_lines = []

    # Сохраняет исходный порядок и комментарии
    for line in env_lines:
        line_s = line.strip()
        if line_s and not line_s.startswith("#") and "=" in line_s:
            k, _ = line_s.split("=", 1)
            k = k.strip()
            if k in env_dict:
                val = env_dict[k]
                output_lines.append(f"{k}={val}\n" if not val.startswith('"') else f"{k}={val}\n")
                written_keys.add(k)
                continue
        output_lines.append(line)

    # Добавляет ключи, которых не было в исходном файле
    missing_keys = [k for k in env_dict if k not in written_keys]
    if missing_keys:
        output_lines.append("\n# Security Hardening & Zero-Trust In-Transit TLS\n")
        for k in missing_keys:
            val = env_dict[k]
            output_lines.append(f"{k}={val}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(output_lines)

    try:
        os.chmod(env_path, 0o600)
    except Exception:
        pass

    print("[+] Конфигурация безопасности .env успешно инициализирована (Zero-Trust TLS).")
    if initial_admin_password:
        password_path = store_initial_admin_password(project_dir, initial_admin_password, initial_password_file)
        generated_info["INITIAL_ADMIN_PASSWORD"] = "сохранён в файл с правами 0600"
        print(f"[+] Первичный пароль администратора записан в {password_path}.")
        print("    Логин: admin. Пароль в журнал не выводится.")

    return generated_info


if __name__ == "__main__":
    setup_vds()
