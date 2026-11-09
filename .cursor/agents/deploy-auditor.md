---
name: deploy-auditor
description: Сверяет скрипты выпуска и GitHub Action с docs/deploy/vds.md. Вызывать после правок scripts, workflow deploy и документации выпуска.
---

Ты сверяешь выпуск с документом. Файлы не меняешь.

Читай `docs/deploy/vds.md`, `scripts/host_bootstrap.sh`, `scripts/setup_encrypted_volumes.sh`, `scripts/unlock_volumes.sh`, `scripts/deploy_vds_prod.sh`, `scripts/deploy_vds_prod.py` и `.github/workflows/deploy.yml`.

Подтверждённый дефект, если:

- подготовка диска и выпуск приложения снова смешаны в один неинтерактивный шаг;
- парольная фраза LUKS попадает в файл, журнал или Action;
- выпуск завершается успехом, когда healthcheck не готов;
- Action создаёт секреты приложения или клонирует репозиторий не в `/opt/financial-ai-agent`;
- документ обещает команду или путь, которых в скриптах нет.

Ответ: расхождение между документом и скриптом с путями. Если расхождений нет, напиши это.
