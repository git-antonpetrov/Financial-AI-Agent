FROM python:3.11-slim

WORKDIR /app

# Копируем зависимости воркера
COPY src/simulations/workers/ruble_eod/requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY src/simulations /app/src/simulations

# Добавляем корневую директорию в PYTHONPATH
ENV PYTHONPATH=/app

CMD ["python", "src/simulations/workers/ruble_eod/main.py"]
