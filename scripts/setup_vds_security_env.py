#!/usr/bin/env python3
"""
Скрипт первоначальной инициализации обязательных переменных окружения безопасности на сервере (VDS):
- Генерация 2048-битной RSA-пары для асимметричной подписи JWT (RS256);
- Генерация 256-битного мастер-ключа шифрования Data at Rest (DATA_ENCRYPTION_KEY);
- Генерация секрета 2FA TOTP (ADMIN_TOTP_SECRET);
- Настройка параметров Caddy и mTLS.
"""

import os
import subprocess
import base64
import secrets

def setup_vds():
    project_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    os.chdir(project_dir)

    certs_dir = os.path.join(project_dir, "certs")
    os.makedirs(certs_dir, exist_ok=True)

    jwt_priv_path = os.path.join(certs_dir, "jwt_private.pem")
    jwt_pub_path = os.path.join(certs_dir, "jwt_public.pem")

    # 1. Генерация RSA ключей если отсутствуют
    if not os.path.exists(jwt_priv_path):
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", jwt_priv_path], check=True)
        os.chmod(jwt_priv_path, 0o600)
    
    if not os.path.exists(jwt_pub_path):
        subprocess.run(["openssl", "rsa", "-in", jwt_priv_path, "-pubout", "-out", jwt_pub_path], check=True)
        os.chmod(jwt_pub_path, 0o644)

    with open(jwt_priv_path, "r", encoding="utf-8") as f:
        priv_pem = f.read().strip().replace("\n", "\\n")

    with open(jwt_pub_path, "r", encoding="utf-8") as f:
        pub_pem = f.read().strip().replace("\n", "\\n")

    data_encryption_key = secrets.token_hex(32)
    totp_secret = base64.b32encode(secrets.token_bytes(20)).decode("utf-8").rstrip("=")

    env_path = os.path.join(project_dir, ".env")
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = [
                line for line in f
                if not any(
                    line.startswith(prefix)
                    for prefix in [
                        "JWT_PRIVATE_KEY=",
                        "JWT_PUBLIC_KEY=",
                        "DATA_ENCRYPTION_KEY=",
                        "ADMIN_TOTP_SECRET=",
                        "REQUIRE_2FA=",
                        "CADDY_CLIENT_AUTH_MODE=",
                        "REQUIRE_MTLS=",
                        "# Security Hardening",
                    ]
                )
            ]

    new_entries = [
        "\n# Security Hardening Keys\n",
        f'JWT_PRIVATE_KEY="{priv_pem}"\n',
        f'JWT_PUBLIC_KEY="{pub_pem}"\n',
        f'DATA_ENCRYPTION_KEY="{data_encryption_key}"\n',
        f'ADMIN_TOTP_SECRET="{totp_secret}"\n',
        "REQUIRE_2FA=false\n",
        "CADDY_CLIENT_AUTH_MODE=request\n",
        "REQUIRE_MTLS=false\n",
    ]

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines + new_entries)

    print("[+] VDS security environment initialized successfully in .env")

if __name__ == "__main__":
    setup_vds()
