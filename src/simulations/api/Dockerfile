FROM python:3.11-slim

WORKDIR /app

COPY src/simulations/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь исходный код проекта (чтобы API имело доступ к src.simulations.db)
COPY src/simulations /app/src/simulations

ENV PYTHONPATH=/app

CMD ["uvicorn", "src.simulations.api.main:app", "--host", "0.0.0.0", "--port", "8002"]
