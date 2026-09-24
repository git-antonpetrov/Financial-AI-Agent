FROM gramineproject/gramine:latest

# Устанавливаем Python 3, pip и openssl (для генерации самоподписанных сертификатов)
RUN apt-get update && \
    apt-get install -y python3 python3-pip openssl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копируем зависимости
COPY requirements.txt .

# Устанавливаем зависимости Python. Флаг --break-system-packages нужен для современных Ubuntu.
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt || \
    pip3 install --no-cache-dir -r requirements.txt

# Копируем код микросервиса и генератор манифеста
COPY signer.py .
COPY build_manifest.py .

# Создаем непривилегированного пользователя
RUN useradd -m -s /bin/bash appuser

# Генерируем самоподписанный SSL-сертификат в папке /certs
# При монтировании named volume Docker скопирует эти файлы в volume
RUN mkdir -p /certs && \
    openssl req -x509 -newkey rsa:4096 -nodes -out /certs/cert.pem -keyout /certs/key.pem -days 365 -subj "/CN=enclave-signer" && \
    chown -R appuser:appuser /certs

# Создаем копию бинарника, чтобы Gramine нашел его рядом с манифестом
RUN cp /usr/bin/python3 /app/signer && \
    python3 build_manifest.py

# Компилируем шаблон
RUN gramine-manifest signer.manifest.template signer.manifest

# Подписываем манифест фейковым ключом для имитации SGX
# (Даже в режиме симуляции gramine-direct требует подписанного манифеста)
RUN gramine-sgx-gen-private-key && \
    gramine-sgx-sign --manifest signer.manifest --output signer.manifest.sgx

# Открываем порт для HTTPS
EXPOSE 8080

RUN chown -R appuser:appuser /app
USER appuser

# Запускаем микросервис внутри симуляции анклава Gramine (без аппаратной поддержки)
ENTRYPOINT ["/usr/bin/gramine-direct", "signer"]
