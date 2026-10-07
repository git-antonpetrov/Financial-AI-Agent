FROM python:3.11-slim

WORKDIR /app

# Установка системных зависимостей для сборки некоторых Python пакетов (ChromaDB)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Создание непривилегированного пользователя appuser
RUN groupadd -g 10001 appuser && \
    useradd -u 10001 -g 10001 -s /sbin/nologin -M -d /app appuser

COPY src/admin_server/vectors/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование общих библиотек и воркера
COPY --chown=appuser:appuser src/common /app/src/common
COPY --chown=appuser:appuser src/admin_server/core /app/core
COPY --chown=appuser:appuser src/admin_server/vectors /app

ENV PYTHONPATH=/app:/app/src

USER appuser

CMD ["python", "vector_worker.py"]
