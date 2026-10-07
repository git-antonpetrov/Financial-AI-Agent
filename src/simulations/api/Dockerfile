FROM python:3.11-slim

WORKDIR /app

COPY src/simulations/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем общие модули и исходный код проекта
COPY src/common /app/src/common
COPY src/simulations /app/src/simulations

ENV PYTHONPATH=/app

CMD ["uvicorn", "src.simulations.api.main:app", "--host", "0.0.0.0", "--port", "8002"]
