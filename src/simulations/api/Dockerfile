FROM python:3.11-slim

WORKDIR /app

# Создание непривилегированного пользователя appuser (SEC-21)
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /sbin/nologin -M -d /app appuser

COPY src/simulations/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем общие модули и исходный код проекта
COPY --chown=appuser:appgroup src/common /app/src/common
COPY --chown=appuser:appgroup src/simulations /app/src/simulations

ENV PYTHONPATH=/app

USER appuser

CMD ["uvicorn", "src.simulations.api.main:app", "--host", "0.0.0.0", "--port", "8002"]
