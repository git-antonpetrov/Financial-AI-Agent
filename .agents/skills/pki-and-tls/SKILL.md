---
name: pki-and-tls
description: Выпуск сертификатов и TLS между сервисами. Применять при правке src/pki, томов сертификатов, Caddyfile и клиентов PostgreSQL, Redis и MinIO.
---

# PKI и TLS

Корневой центр — контейнер `root-ca`. Он завершается после выпуска и не обслуживает прикладные запросы. Ключ корня шифруется `ROOT_CA_PASSPHRASE`.

Каждый сервис получает свой том: `admin_certs`, `bank_certs`, `postgres_certs`, `redis_certs`, `minio_certs`, `ca_public`. Ключ клиента администратора в общий экспорт не кладут: у `root-ca` включено `ISOLATE_ADMIN_CLIENT_KEY=true`.

Публичное имя Caddy берёт сертификат из `admin_certs`. Имена `simulation-api.internal` и `bank-service.internal` используют `tls internal`. `SSL_CERT_FILE` указывает на `ca.crt` платформы, не на внутренний корень Caddy.

`FORCE_REGENERATE=true` переписывает уже выпущенные сертификаты. Без явной просьбы его не включают.

Хостовый каталог `certs/` боевой контур не использует. Скрипт выпуска удаляет его после проверки режима `0600` у найденных ключей.
