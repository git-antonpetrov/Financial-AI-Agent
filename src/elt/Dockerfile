FROM python:3.14-slim

WORKDIR /app

# Устанавливаем системные зависимости (poppler для PDF, libreoffice для .doc/.docx)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    poppler-utils \
    libreoffice \
    && rm -rf /var/lib/apt/lists/*

COPY src/elt/requirements.txt .

# Устанавливаем Python-зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код и промпты
COPY src/ ./src/
COPY prompts/ ./prompts/

# Задаем переменные окружения
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Запуск фонового скрипта оркестратора
CMD ["python", "src/elt/scripts/extract/update_parsers.py"]
