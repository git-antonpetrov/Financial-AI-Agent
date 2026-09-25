FROM python:3.11-slim

WORKDIR /app

# Установка системных зависимостей для сборки некоторых Python пакетов (ChromaDB)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "vector_worker.py"]
