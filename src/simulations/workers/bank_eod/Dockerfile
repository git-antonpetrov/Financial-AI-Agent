FROM python:3.11-slim

WORKDIR /app

# Копируем зависимости воркера
COPY src/simulations/workers/bank_eod/requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
# Мы копируем папку src целиком, так как воркеру нужны модели БД из src/simulations/db/bank
COPY src/ /app/src/

# Добавляем корневую директорию в PYTHONPATH
ENV PYTHONPATH=/app

# В реальном проде здесь стоял бы cron, celery или apscheduler.
# Пока что контейнер просто выполнит EOD и завершится (или его можно запускать по расписанию в docker-compose)
CMD ["python", "src/simulations/workers/bank_eod/main.py"]
