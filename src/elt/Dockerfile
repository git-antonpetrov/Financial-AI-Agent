FROM python:3.14-slim

WORKDIR /app

# Устанавливаем системные зависимости (poppler для PDF, libreoffice для .doc/.docx)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    poppler-utils \
    libreoffice \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Устанавливаем Python-зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY src/ ./src/

# Задаем переменные окружения, чтобы скрипт знал, где искать папки и как логироваться
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Запуск фонового скрипта оркестратора
CMD ["python", "src/elt/extract/update_parsers.py"]
