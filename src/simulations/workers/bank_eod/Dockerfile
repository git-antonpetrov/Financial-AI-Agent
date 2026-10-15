FROM python:3.11-slim

WORKDIR /app

# Создание непривилегированного пользователя appuser (SEC-21)
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /sbin/nologin -M -d /app appuser

# Копируем зависимости воркера
COPY src/simulations/workers/bank_eod/requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY --chown=appuser:appgroup src/common /app/src/common
COPY --chown=appuser:appgroup src/simulations /app/src/simulations

# Добавляем корневую директорию в PYTHONPATH
ENV PYTHONPATH=/app

USER appuser

# Запуск фонового планировщика EOD задач (AsyncIOScheduler)
CMD ["python", "src/simulations/workers/bank_eod/main.py"]
