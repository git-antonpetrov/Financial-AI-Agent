#!/usr/bin/env python3
"""
Скрипт первоначальной инициализации обязательных переменных окружения безопасности на сервере (VDS):
- Генерация 2048-битной RSA-пары для асимметричной подписи JWT (RS256);
- Генерация 256-битного мастер-ключа шифрования Data at Rest (DATA_ENCRYPTION_KEY);
- Генерация секрета 2FA TOTP (ADMIN_TOTP_SECRET);
- Генерация криптостойких паролей для БД, Redis, MinIO и ChromaDB (если заданы заглушки);
- Настройка параметров сквозного Zero-Trust шифрования (POSTGRES_SSLMODE=require, REDIS_SSL=true, MINIO_SECURE=true);
- Настройка параметров Caddy и mTLS.
"""

import os
import sys
import base64
import secrets
import subprocess
from pathlib import Path


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


def setup_vds(project_dir: str | None = None) -> dict[str, str]:
    """
    Выполняет инициализацию параметров безопасности в .env:
    - Создает .env из .env.example, если файл отсутствует;
    - Генерирует асимметричную пару RSA-2048 для подписи JWT в памяти и сохраняет в .env;
    - Заменяет плейсхолдеры на криптографически стойкие секреты;
    - Устанавливает флаги Zero-Trust TLS;
    - Возвращает словарь с ключевыми сгенерированными учетными данными.
    """
    if project_dir is None:
        project_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    else:
        project_dir = os.path.abspath(project_dir)

    env_path = os.path.join(project_dir, ".env")
    example_path = os.path.join(project_dir, ".env.example")

    env_lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            env_lines = f.readlines()
    elif os.path.exists(example_path):
        with open(example_path, "r", encoding="utf-8") as f:
            env_lines = f.readlines()

    # Парсим существующие ключи
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
    if not cur_jwt_priv or not cur_jwt_pub or "BEGIN RSA PRIVATE KEY" in cur_jwt_priv:
        priv_pem, pub_pem = generate_rsa_keypair()
        generated_info["JWT_KEY_ALGORITHM"] = "RS256 (2048-bit in-memory)"
    else:
        priv_pem = cur_jwt_priv.strip('"')
        pub_pem = cur_jwt_pub.strip('"')

    # 2. Мастер-ключ шифрования Data at Rest (AES-256-GCM)
    cur_enc_key = env_dict.get("DATA_ENCRYPTION_KEY", "")
    if not cur_enc_key or cur_enc_key.startswith("0123456789abcdef"):
        env_dict["DATA_ENCRYPTION_KEY"] = secrets.token_hex(32)
        generated_info["DATA_ENCRYPTION_KEY"] = "Сгенерирован новый 256-битный ключ AES-GCM"

    # 3. TOTP секрет для 2FA (RFC 6238)
    cur_totp = env_dict.get("ADMIN_TOTP_SECRET", "")
    if not cur_totp or cur_totp.startswith("JBSWY3DPEHPK3PXP"):
        env_dict["ADMIN_TOTP_SECRET"] = base64.b32encode(secrets.token_bytes(20)).decode("utf-8").rstrip("=")
        generated_info["ADMIN_TOTP_SECRET"] = "Сгенерирован новый Base32 TOTP секрет"

    # 4. Пароли инфраструктурных сервисов
    placeholders = {
        "POSTGRES_PASSWORD": ("change_this_strong_password_in_production", lambda: secrets.token_urlsafe(24)),
        "REDIS_PASSWORD": ("change_this_redis_password", lambda: secrets.token_urlsafe(24)),
        "PGADMIN_DEFAULT_PASSWORD": ("change_this_pgadmin_password", lambda: secrets.token_urlsafe(24)),
        "MINIO_ACCESS_KEY": ("minio_access_key_placeholder", lambda: secrets.token_hex(12)),
        "MINIO_SECRET_KEY": ("minio_secret_key_placeholder", lambda: secrets.token_urlsafe(32)),
        "CHROMA_AUTH_TOKEN": ("chroma_token_secret_placeholder", lambda: secrets.token_urlsafe(24)),
        "AGENT_DIGITAL_BOOTSTRAP_TOKEN": ("bootstrap_token_digital_agent_secret", lambda: secrets.token_urlsafe(32)),
        "AGENT_BANK_BOOTSTRAP_TOKEN": ("bootstrap_token_bank_agent_secret", lambda: secrets.token_urlsafe(32)),
        "AGENT_INVEST_BOOTSTRAP_TOKEN": ("bootstrap_token_invest_agent_secret", lambda: secrets.token_urlsafe(32)),
        "AGENT_MAIN_BOOTSTRAP_TOKEN": ("bootstrap_token_main_agent_secret", lambda: secrets.token_urlsafe(32)),
    }

    for key, (placeholder_val, gen_fn) in placeholders.items():
        cur_val = env_dict.get(key, "")
        if not cur_val or cur_val == placeholder_val:
            new_val = gen_fn()
            env_dict[key] = new_val
            generated_info[key] = "Сгенерирован надежный случайный пароль/токен"

    # 5. MinIO KMS ключ
    cur_kms = env_dict.get("MINIO_KMS_SECRET_KEY", "")
    if not cur_kms or "0123456789abcdef" in cur_kms:
        env_dict["MINIO_KMS_SECRET_KEY"] = f"financial-kms-key:{secrets.token_hex(32)}"
        generated_info["MINIO_KMS_SECRET_KEY"] = "Сгенерирован ключ MinIO KMS SSE-S3"

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
            generated_info["INITIAL_ADMIN_PASSWORD"] = initial_admin_password
        except Exception:
            pass

    # 7. Принудительные параметры Zero-Trust In-Transit TLS & RSA JWT
    env_dict["JWT_PRIVATE_KEY"] = f'"{priv_pem}"'
    env_dict["JWT_PUBLIC_KEY"] = f'"{pub_pem}"'
    env_dict["POSTGRES_SSLMODE"] = "require"
    env_dict["REDIS_SSL"] = "true"
    env_dict["MINIO_SECURE"] = "true"
    env_dict["MINIO_SSE_ENABLED"] = "true"
    env_dict["CHROMA_ENVELOPE_ENCRYPTION_ENABLED"] = "true"
    env_dict["CADDY_CLIENT_AUTH_MODE"] = env_dict.get("CADDY_CLIENT_AUTH_MODE", "request")
    env_dict["REQUIRE_MTLS"] = env_dict.get("REQUIRE_MTLS", "false")
    env_dict["REQUIRE_2FA"] = env_dict.get("REQUIRE_2FA", "false")

    # Перезаписываем или обновляем .env
    written_keys = set()
    output_lines = []

    # Сохраняем исходный порядок и комментарии
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

    # Добавляем ключи, которых не было в исходном файле
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
        print("=" * 70)
        print(" [!] ВНИМАНИЕ: Сгенерирован первичный пароль супер-администратора:")
        print(f"     Логин:  admin")
        print(f"     Пароль: {initial_admin_password}")
        print("     ОБЯЗАТЕЛЬНО сохраните эти учетные данные!")
        print("=" * 70)

    return generated_info


if __name__ == "__main__":
    setup_vds()

