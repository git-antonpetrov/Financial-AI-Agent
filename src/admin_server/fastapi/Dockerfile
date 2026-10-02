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
COPY fastapi/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование исходного кода с назначением прав
COPY --chown=appuser:appuser fastapi/ .
COPY --chown=appuser:appuser core/ core/

# Переключение на непривилегированного пользователя
USER appuser

# Запуск сервера
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
