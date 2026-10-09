#!/usr/bin/env python3
"""
Скрипт начальной инициализации и автоматического выпуска инфраструктурных сертификатов (Bootstrap PKI).
Может запускаться как автономная утилита на хосте, так и внутри контейнера Root CA.
"""

import datetime
import hashlib
import os
import shutil
import sys
from pathlib import Path
from typing import Dict, Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes

try:
    from src.pki.core.ca_engine import RootCAEngine
    from src.pki.core.cert_issuer import CertificateIssuer
    from src.pki.core.passphrase_vault import resolve_root_ca_passphrase
except ImportError:
    try:
        from .core.ca_engine import RootCAEngine
        from .core.cert_issuer import CertificateIssuer
        from .core.passphrase_vault import resolve_root_ca_passphrase
    except ImportError:
        from core.ca_engine import RootCAEngine
        from core.cert_issuer import CertificateIssuer
        from core.passphrase_vault import resolve_root_ca_passphrase


def get_cert_fingerprint(cert: x509.Certificate) -> str:
    """Возвращает форматированный SHA-256 отпечаток сертификата."""
    raw_digest = cert.fingerprint(hashes.SHA256())
    return ":".join(f"{b:02X}" for b in raw_digest)


def bootstrap_pki(
    data_dir: Path | str,
    export_dir: Path | str,
    passphrase: str | None = None,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Выполняет полный цикл инициализации Root CA и выпуска сертификатов для всех сервисов.
    """
    data_path = Path(data_dir).resolve()
    export_path = Path(export_dir).resolve()

    data_path.mkdir(parents=True, exist_ok=True)
    export_path.mkdir(parents=True, exist_ok=True)

    actual_passphrase = passphrase or resolve_root_ca_passphrase()

    print("=================================================================")
    print("      ZERO-TRUST PKI BOOTSTRAP ENGINE: ИНИЦИАЛИЗАЦИЯ             ")
    print("=================================================================")
    print(f"[*] Директория изолированных данных CA: {data_path}")
    print(f"[*] Директория экспорта сертификатов:   {export_path}")

    # 1. Инициализация или загрузка Root CA
    ca_engine = RootCAEngine(
        data_dir=data_path,
        passphrase=actual_passphrase,
        key_size=4096,
        validity_years=10,
    )

    if force and ca_engine.key_path.exists():
        print("[!] Принудительная перегенерация Root CA (force=True)...")
        root_cert, _ = ca_engine.initialize(force=True)
    else:
        root_cert, _ = ca_engine.ensure_initialized()

    # Экспорт корневого публичного сертификата
    ca_export_target = export_path / "ca.crt"
    ca_engine.export_ca_certificate(ca_export_target)
    print(f"[+] Корневой сертификат ca.crt успешно экспортирован -> {ca_export_target}")
    print(f"    - Serial:       {root_cert.serial_number}")
    print(f"    - Fingerprint:  {get_cert_fingerprint(root_cert)}")
    print(f"    - Valid Until:  {root_cert.not_valid_after_utc.isoformat()}")

    issuer = CertificateIssuer(ca_engine)

    manifest = {
        "root_ca": {
            "serial": root_cert.serial_number,
            "fingerprint": get_cert_fingerprint(root_cert),
            "valid_until": root_cert.not_valid_after_utc.isoformat(),
        },
        "services": {},
    }

    # Спецификация сертификатов узлов платформы
    specs = [
        # 1. Банковский шлюз (Simulation API)
        {
            "name": "bank",
            "cert_filename": "bank.crt",
            "key_filename": "bank.key",
            "cn": "bank-simulation",
            "role": "bank_service",
            "san_dns": [
                "bank-simulation",
                "bank-service.internal",
                "simulation-api",
                "simulation-api.internal",
                "localhost",
            ],
            "san_ip": ["127.0.0.1"],
            "is_server": True,
            "is_client": True,
            "validity_days": 365,
        },
        # 2. Сервер Администратора (Admin Server)
        {
            "name": "admin-server",
            "cert_filename": "admin_server.crt",
            "key_filename": "admin_server.key",
            "cn": "admin-server",
            "role": "Security Operations",
            "san_dns": [
                "admin-server",
                "admin-server.internal",
                "admin.fin-ai-agent.local",
                "localhost",
            ],
            "san_ip": ["127.0.0.1"],
            "is_server": True,
            "is_client": True,
            "validity_days": 365,
        },
        # 3. Клиент Администратора (Admin Client mTLS)
        {
            "name": "admin-client",
            "cert_filename": "admin_client.crt",
            "key_filename": "admin_client.key",
            "cn": "superadmin-workstation",
            "role": "admin_operator",
            "san_dns": ["localhost"],
            "san_ip": ["127.0.0.1"],
            "is_server": False,
            "is_client": True,
            "validity_days": 365,
        },
        # 4. PostgreSQL Database
        {
            "name": "postgres",
            "cert_filename": "postgres.crt",
            "key_filename": "postgres.key",
            "cn": "postgres-db",
            "role": "database",
            "san_dns": ["postgres-db", "localhost"],
            "san_ip": ["127.0.0.1"],
            "is_server": True,
            "is_client": False,
            "validity_days": 365,
        },
        # 5. Redis Server
        {
            "name": "redis",
            "cert_filename": "redis.crt",
            "key_filename": "redis.key",
            "cn": "redis-server",
            "role": "cache_and_security",
            "san_dns": ["redis", "redis-server", "localhost"],
            "san_ip": ["127.0.0.1"],
            "is_server": True,
            "is_client": True,
            "validity_days": 365,
        },
        # 6. MinIO Object Storage
        {
            "name": "minio",
            "cert_filename": "minio.crt",
            "key_filename": "minio.key",
            "cn": "minio",
            "role": "object_storage",
            "san_dns": ["minio", "minio-server", "localhost"],
            "san_ip": ["127.0.0.1"],
            "is_server": True,
            "is_client": False,
            "validity_days": 365,
        },
    ]

    print("\n[*] Выпуск и обновление сервисных сертификатов:")
    for spec in specs:
        cert_file = export_path / spec["cert_filename"]
        key_file = export_path / spec["key_filename"]

        # Если сертификат уже существует и не требуется force — проверяем валидность
        if cert_file.exists() and key_file.exists() and not force:
            try:
                existing_cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
                # Проверяем, не истекает ли в ближайшие 30 дней
                now = datetime.datetime.now(datetime.timezone.utc)
                if existing_cert.not_valid_after_utc > now + datetime.timedelta(days=30):
                    print(f"  [✓] {spec['name']:<14} Сертификат валиден (до {existing_cert.not_valid_after_utc.strftime('%Y-%m-%d')})")
                    manifest["services"][spec["name"]] = {
                        "cert": str(cert_file),
                        "serial": existing_cert.serial_number,
                        "status": "VALID",
                    }
                    continue
            except Exception:
                pass  # При повреждении или протухании перевыпускаем

        cert, key = issuer.issue_certificate(
            common_name=spec["cn"],
            role=spec["role"],
            san_dns_names=spec.get("san_dns"),
            san_ip_addresses=spec.get("san_ip"),
            validity_days=spec["validity_days"],
            key_size=2048,
            is_server=spec["is_server"],
            is_client=spec["is_client"],
        )

        issuer.save_cert_and_key(cert, key, cert_file, key_file)
        fp = get_cert_fingerprint(cert)
        print(f"  [+] {spec['name']:<14} Выпущен новый сертификат (Serial: {cert.serial_number}, FP: {fp[:17]}...)")

        manifest["services"][spec["name"]] = {
            "cert": str(cert_file),
            "key": str(key_file),
            "serial": cert.serial_number,
            "fingerprint": fp,
            "status": "ISSUED",
        }

    # 3. Специальная адаптация под структуру томов MinIO
    minio_dir = export_path / "minio"
    minio_cas = minio_dir / "CAs"
    minio_cas.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(export_path / "minio.crt", minio_dir / "public.crt")
    shutil.copyfile(export_path / "minio.key", minio_dir / "private.key")
    shutil.copyfile(export_path / "ca.crt", minio_cas / "ca.crt")
    print("[+] Структура каталогов для MinIO успешно синхронизирована (certs/minio).")

    # 4. Обратная совместимость с именами server.crt / server.key и client.crt / client.key
    if not (export_path / "server.crt").exists() or force:
        shutil.copyfile(export_path / "admin_server.crt", export_path / "server.crt")
        shutil.copyfile(export_path / "admin_server.key", export_path / "server.key")
    if not (export_path / "client.crt").exists() or force:
        shutil.copyfile(export_path / "admin_client.crt", export_path / "client.crt")
        shutil.copyfile(export_path / "admin_client.key", export_path / "client.key")

    print("\n[✓] Инициализация PKI успешно завершена. Все сервисы обеспечены доверенными сертификатами.")
    print("=================================================================\n")
    return manifest


if __name__ == "__main__":
    default_data = os.getenv("ROOT_CA_DATA_DIR", "/data/ca")
    default_export = os.getenv("SHARED_CERTS_DIR", "/shared_certs")

    force_flag = os.getenv("FORCE_REGENERATE", "false").lower() in ("true", "1")
    bootstrap_pki(default_data, default_export, force=force_flag)
