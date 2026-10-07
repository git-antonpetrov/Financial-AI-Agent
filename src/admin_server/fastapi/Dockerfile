FROM python:3.11-slim

WORKDIR /app

# Установка системных зависимостей
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Создание непривилегированного пользователя appuser
RUN groupadd -g 10001 appuser && \
    useradd -u 10001 -g 10001 -s /sbin/nologin -M -d /app appuser

# Установка Python зависимостей
COPY src/admin_server/fastapi/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование общих библиотек и сервисного кода
COPY --chown=appuser:appuser src/common /app/src/common
COPY --chown=appuser:appuser src/admin_server/core /app/core
COPY --chown=appuser:appuser src/admin_server/fastapi /app

# Настройка путей импорта
ENV PYTHONPATH=/app:/app/src

# Переключение на непривилегированного пользователя
USER appuser

# Запуск сервера
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
