FROM python:3.11-slim

WORKDIR /app

# Установка системных зависимостей для сборки некоторых Python пакетов (ChromaDB)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Создание непривилегированного пользователя appuser
RUN groupadd -g 10001 appuser && \
    useradd -u 10001 -g 10001 -s /sbin/nologin -M -d /app appuser

COPY vectors/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser:appuser vectors/ .
COPY --chown=appuser:appuser core/ core/

USER appuser

CMD ["python", "vector_worker.py"]
