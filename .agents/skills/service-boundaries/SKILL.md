---
name: service-boundaries
description: Границы каталогов src. Применять при новом импорте между admin_server, admin_client, simulations, pki и gateways.
---

# Границы сервисов

Карта каталогов — `docs/src.md`.

`admin_server`, `admin_client`, `simulations` и `pki` не импортируют внутренности друг друга. Общий код берётся из `src/common`.

Caddy — только `src/caddy/Caddyfile`. Шлюзы Gemini и Tavily — `src/gateways` и в Compose не входят.

Деньги и контракты живут в `src/simulations`. Административный API и RAG — в `src/admin_server`. Рабочая станция — в `src/admin_client`. Выпуск сертификатов — в `src/pki`.

Новый общий помощник кладут в `src/common`, а не копируют в два сервиса. Секрет одного сервиса не читают из каталога другого.
