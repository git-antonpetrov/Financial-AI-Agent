---
name: deploy
description: Выпуск на VDS. Применять при правке scripts/host_bootstrap.sh, setup_encrypted_volumes.sh, unlock_volumes.sh, deploy_vds_prod.sh, deploy_vds_prod.py и docs/deploy.
---

# Выпуск

Подготовка диска и выпуск приложения разделены.

`sudo bash scripts/host_bootstrap.sh` ставят один раз в интерактивной сессии root. Он создаёт LUKS2 `aes-xts` с ключом 512 бит, переносит `data-root` Docker на `/mnt/fin_agent_encrypted_volumes/docker` и ссылку `/opt/financial-ai-agent`. Парольную фразу на диск не пишет. Docker в автозапуск не ставит.

После перезагрузки том открывает `sudo bash scripts/unlock_volumes.sh`.

`bash scripts/deploy_vds_prod.sh` только ставит `cryptography` и `bcrypt` в `.venv` и вызывает `scripts/deploy_vds_prod.py`. Оркестратор проверяет, что проект и Docker на одном открытом LUKS, заполняет `.env`, требует ключ GCP режима `0600` и поднимает Compose. Неготовый healthcheck завершает процесс с кодом 1.

GitHub Action в `.github/workflows/deploy.yml` только обновляет `/opt/financial-ai-agent` и запускает этот скрипт. Секретов приложения он не создаёт. Токен для `git fetch` живёт на сервере.

Профили `seed` и `tools` сами не стартуют. Шлюзы Cloudflare этот сценарий не выкладывает.
