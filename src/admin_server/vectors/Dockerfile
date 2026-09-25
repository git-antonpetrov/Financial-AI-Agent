FROM python:3.11-slim

WORKDIR /app

# Установка системных зависимостей для сборки некоторых Python пакетов (ChromaDB)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY vectors/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vectors/ .
COPY core/ core/

CMD ["python", "vector_worker.py"]
