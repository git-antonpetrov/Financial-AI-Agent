FROM python:3.14-slim

WORKDIR /app

# Настройки для Python (без буферизации, чтобы логи сразу шли в консоль)
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Копируем requirements
COPY src/mcp_servers/rag/requirements.txt /app/requirements.txt

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY src/ ./src/

ENV PYTHONPATH=/app

# Открываем порт 8001
EXPOSE 8001

# Запуск сервера
CMD ["python", "-m", "src.mcp_servers.rag.server"]
