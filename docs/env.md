# Переменные окружения

Настоящий документ перечисляет переменные из `.env.example`. Файл `.env` в git не входит. На VDS его создаёт выпуск в каталоге на LUKS. Локально его создают копированием примера и `scripts/setup_vds_security_env.py`.

Скрипт заменяет известные плейсхолдеры и не печатает пароль администратора. Уже случайные значения он не перезаписывает. Плейсхолдеры из примера в боевой `.env` оставлять нельзя: строгий режим их отклоняет.

---

## 1. Модели и поиск

| Переменная | Назначение |
| --- | --- |
| `VERTEX_PROJECT` | Проект Google Cloud |
| `VERTEX_LOCATION` | Регион моделей. В примере `global` |
| `VERTEX_BASE_URL` | Базовый адрес шлюза Gemini, а не прямой адрес Google |
| `MAIN_MODEL_NAME` | Модель основного агента для LiteLLM |
| `MAIN_REASONING_EFFORT` | Усилие рассуждения основной модели |
| `RAG_DATA_MODEL_NAME` | Модель разбора документов RAG |
| `RAG_DATA_REASONING_EFFORT` | Усилие рассуждения модели RAG |
| `TAVILY_API_KEY` | Ключ поискового API |
| `TAVILY_BASE_URL` | Базовый адрес шлюза Tavily |

Шлюзы лежат в `src/gateways` и на VDS этим репозиторием не выкладываются.

---

## 2. PostgreSQL и pgAdmin

| Переменная | Назначение |
| --- | --- |
| `POSTGRES_USER` | Пользователь административной базы |
| `POSTGRES_PASSWORD` | Пароль этого пользователя |
| `POSTGRES_DB` | Имя административной базы |
| `POSTGRES_SSLMODE` | Режим TLS. Боевое значение `verify-full` |
| `BANK_DB_PASSWORD` | Пароль роли `bank_app` |
| `INVEST_DB_PASSWORD` | Пароль роли `invest_app` |
| `DIGITAL_DB_PASSWORD` | Пароль роли `digital_app` |
| `PGADMIN_DEFAULT_EMAIL` | Логин pgAdmin. Сервис в профиле `tools` |
| `PGADMIN_DEFAULT_PASSWORD` | Пароль pgAdmin |

---

## 3. Вход администратора и JWT

| Переменная | Назначение |
| --- | --- |
| `JWT_PRIVATE_KEY` | Закрытый ключ RS256 в одной строке PEM |
| `JWT_PUBLIC_KEY` | Открытый ключ RS256 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Срок доступа, в примере 15 минут |
| `REFRESH_TOKEN_EXPIRE_DAYS` | Срок обновления, в примере 7 дней |
| `ADMIN_TOTP_SECRET` | Секрет TOTP в Base32 |
| `REQUIRE_2FA` | Требовать второй фактор |
| `ADMIN_PASSWORD_HASH` | Хэш bcrypt пароля администратора |

Пути `JWT_PRIVATE_KEY_PATH` и `JWT_PUBLIC_KEY_PATH` в примере закомментированы. В Compose административный сервер по умолчанию читает ключ сервиса из тома сертификатов.

---

## 4. Корень PKI и граница

| Переменная | Назначение |
| --- | --- |
| `ROOT_CA_PASSPHRASE` | Парольная фраза закрытого ключа корневого центра |
| `FORCE_REGENERATE` | Перевыпустить сертификаты при следующем запуске `root-ca` |
| `REQUIRE_MTLS` | Требовать клиентский сертификат |
| `ALLOWED_MTLS_SUBJECTS` | Допустимые субъекты через запятую. Пустое значение скрипт заполняет CN выпущенного клиента или `superadmin-workstation` |
| `ALLOWED_MTLS_ISSUERS` | Допустимые издатели через запятую. Пустой список не сужает издателя |
| `CADDY_CLIENT_AUTH_MODE` | Режим клиента Caddy. Боевое значение `require_and_verify` |
| `PROXY_INTERNAL_SECRET` | Секрет заголовка `X-Proxy-Secret`. Без него Compose не запускает Caddy, административный сервер и шлюз симуляции |
| `ADMIN_DOMAIN` | Публичное имя входа |
| `ADMIN_LOCAL_DOMAIN` | Локальное имя входа |

---

## 5. Шифрование и хранилища

| Переменная | Назначение |
| --- | --- |
| `DATA_ENCRYPTION_KEY` | Мастер-ключ AES-256-GCM, 64 шестнадцатеричных символа |
| `MINIO_ACCESS_KEY` | Имя корневого пользователя MinIO |
| `MINIO_SECRET_KEY` | Пароль MinIO |
| `MINIO_SECURE` | Ходить в MinIO по HTTPS |
| `MINIO_SSE_ENABLED` | Включить шифрование объектов |
| `MINIO_SSE_TYPE` | `s3` или `ssec` |
| `MINIO_KMS_SECRET_KEY` | Ключ SSE-S3 в виде `имя:32 байта Base64` |
| `MINIO_SSE_C_KEY` | Необязательный ключ клиента, если тип `ssec` |
| `REDIS_PASSWORD` | Пароль Redis |
| `REDIS_SSL` | Ходить в Redis по TLS |
| `EMBEDDING_MODEL_NAME` | Модель эмбеддингов |
| `CHUNK_BATCH_SIZE` | Размер пакета нарезки |
| `CHUNK_SLEEP_SECONDS` | Пауза между пакетами |
| `CHUNK_SIZE` | Длина чанка |
| `CHUNK_OVERLAP` | Перехлёст чанков |
| `CHROMA_AUTH_TOKEN` | Токен ChromaDB |
| `CHROMA_ENVELOPE_ENCRYPTION_ENABLED` | Шифровать чанки до записи в ChromaDB |

---

## 6. Агенты, оракул и распознавание

| Переменная | Назначение |
| --- | --- |
| `AGENT_DIGITAL_BOOTSTRAP_TOKEN` | Токен агента цифрового рубля |
| `AGENT_BANK_BOOTSTRAP_TOKEN` | Токен банковского агента |
| `AGENT_INVEST_BOOTSTRAP_TOKEN` | Токен инвестиционного агента |
| `AGENT_MAIN_BOOTSTRAP_TOKEN` | Токен основного агента |
| `ORACLE_BOOTSTRAP_TOKEN` | Токен финансового оракула |
| `AGENT_ORACLE_BOOTSTRAP_TOKEN` | Токен агента оракула |
| `CONTENTAI_USERNAME` | Пользователь Content AI |
| `CONTENTAI_PASSWORD` | Пароль Content AI |
| `CONTENT_AI_API_URI` | Адрес SOAP-сервиса распознавания |

---

## 7. Строгий режим

| Переменная | Назначение |
| --- | --- |
| `STRICT_SECURITY` | Включённый строгий контур. В Compose для сервера и шлюза задано `true` |
| `REQUIRE_ENCLAVE_SIGNATURE` | Требовать подпись программного контура анклава |
| `REQUIRE_ORACLE_SIGNATURE` | Требовать подпись оракула |
| `ISOLATE_ADMIN_CLIENT_KEY` | В примере `false`. У контейнера `root-ca` Compose всё равно задаёт `true`: ключ клиента администратора не кладётся в общий экспорт |
