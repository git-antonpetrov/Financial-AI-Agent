---
name: local-run
description: Локальный запуск Docker Compose без LUKS. Применять, когда работу проверяют на машине разработчика, а не на VDS.
---

# Локальный запуск

На рабочей машине стек поднимают из корня:

```bash
docker compose up -d --build
```

`scripts/deploy_vds_prod.sh` локально не вызывают: он останавливается без открытого LUKS.

`.env` получают копированием `.env.example` и запуском `python scripts/setup_vds_security_env.py`. Без `PROXY_INTERNAL_SECRET` Compose не стартует Caddy и серверы, где секрет обязателен.

Ключ GCP для моделей кладут в `data/gcp-service-account.json`. Без него контейнеры поднимаются, вызовы Vertex завершаются ошибкой.

Наполнение баз: `docker compose --profile seed up simulations-seeder`. pgAdmin: `docker compose --profile tools up -d pgadmin`.

Локальный диск не заменяет LUKS. Для боевых данных этот режим не используют.
