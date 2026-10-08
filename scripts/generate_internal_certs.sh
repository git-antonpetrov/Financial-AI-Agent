#!/usr/bin/env bash
# ==============================================================================
# FINANCIAL AI AGENT - Скрипт автоматизированной генерации внутренних сертификатов
# Root CA, Server Certificate (Caddy TLS) и Client Certificate (mTLS Zero Trust)
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CERTS_DIR="${ROOT_DIR}/certs"

echo "================================================================="
echo "  Генерация криптографических сертификатов и ключей mTLS (PKI)   "
echo "================================================================="

mkdir -p "${CERTS_DIR}"
cd "${CERTS_DIR}"

DAYS_CA=3650       # 10 лет для Root CA
DAYS_CERT=825      # ~2 года для рабочих сертификатов

# ------------------------------------------------------------------------------
# 1. Генерация внутреннего Root Certificate Authority (Root CA)
# ------------------------------------------------------------------------------
echo "[1/4] Генерация внутреннего Root CA (Private Key + Certificate)..."
if [[ ! -f "ca.key" || ! -f "ca.crt" ]]; then
    openssl genrsa -out ca.key 4096
    chmod 600 ca.key
    openssl req -x509 -new -nodes -key ca.key -sha256 -days "${DAYS_CA}" \
        -subj "/C=RU/O=FinancialAI/OU=Security/CN=FinancialAI Internal Root CA" \
        -out ca.crt
    chmod 644 ca.crt
    echo "  -> Успешно создан ca.crt и ca.key"
else
    echo "  -> ca.key и ca.crt уже существуют, пропускаем создание Root CA."
fi

# ------------------------------------------------------------------------------
# 2. Генерация серверного сертификата для Edge Proxy (Caddy / HTTPS)
# ------------------------------------------------------------------------------
echo "[2/4] Генерация серверного сертификата для Caddy Reverse Proxy..."
openssl genrsa -out server.key 2048
chmod 600 server.key

cat > server_ext.cnf << 'EOF'
[req]
default_bits = 2048
prompt = no
default_md = sha256
req_extensions = req_ext
distinguished_name = dn

[dn]
C = RU
O = FinancialAI
OU = Infrastructure
CN = admin.fin-ai-agent.ru

[req_ext]
subjectAltName = @alt_names

[alt_names]
DNS.1 = admin.fin-ai-agent.ru
DNS.2 = localhost
DNS.3 = admin-server
IP.1 = 127.0.0.1
EOF

openssl req -new -key server.key -out server.csr -config server_ext.cnf

cat > server_v3.ext << 'EOF'
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = admin.fin-ai-agent.ru
DNS.2 = localhost
DNS.3 = admin-server
IP.1 = 127.0.0.1
EOF

openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -out server.crt -days "${DAYS_CERT}" -sha256 -extfile server_v3.ext

chmod 644 server.crt
rm -f server.csr server_ext.cnf server_v3.ext
echo "  -> Успешно создан server.crt и server.key"

# ------------------------------------------------------------------------------
# 3. Генерация клиентского сертификата mTLS для оператора / Desktop Client
# ------------------------------------------------------------------------------
echo "[3/4] Генерация клиентского сертификата mTLS (Client Authentication)..."
CLIENT_CN="admin-client"
openssl genrsa -out client.key 2048
chmod 600 client.key

openssl req -new -key client.key -out client.csr \
    -subj "/C=RU/O=FinancialAI/OU=AdminPanel/CN=${CLIENT_CN}"

cat > client_v3.ext << 'EOF'
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
EOF

openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -out client.crt -days "${DAYS_CERT}" -sha256 -extfile client_v3.ext

chmod 644 client.crt
rm -f client.csr client_v3.ext

# Экспорт в PKCS#12 (.p12) для браузеров и настольного приложения
openssl pkcs12 -export -out client.p12 -inkey client.key -in client.crt -certfile ca.crt -passout pass:clientsecret
chmod 600 client.p12
echo "  -> Успешно создан client.crt, client.key и контейнер client.p12 (пароль: clientsecret)"

# ------------------------------------------------------------------------------
# 4. Генерация сертификата для PostgreSQL (In-Transit Encryption)
# ------------------------------------------------------------------------------
echo "[4/7] Генерация серверного сертификата для PostgreSQL..."
openssl genrsa -out postgres.key 2048
chmod 600 postgres.key

cat > postgres_ext.cnf << 'EOF'
[req]
default_bits = 2048
prompt = no
default_md = sha256
req_extensions = req_ext
distinguished_name = dn

[dn]
C = RU
O = FinancialAI
OU = Database
CN = postgres-db

[req_ext]
subjectAltName = @alt_names

[alt_names]
DNS.1 = postgres-db
DNS.2 = financial-postgres
DNS.3 = localhost
IP.1 = 127.0.0.1
EOF

openssl req -new -key postgres.key -out postgres.csr -config postgres_ext.cnf

cat > postgres_v3.ext << 'EOF'
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = postgres-db
DNS.2 = financial-postgres
DNS.3 = localhost
IP.1 = 127.0.0.1
EOF

openssl x509 -req -in postgres.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -out postgres.crt -days "${DAYS_CERT}" -sha256 -extfile postgres_v3.ext

chmod 600 postgres.key
chmod 644 postgres.crt
rm -f postgres.csr postgres_ext.cnf postgres_v3.ext
echo "  -> Успешно создан postgres.crt и postgres.key"

# ------------------------------------------------------------------------------
# 5. Генерация сертификата для Redis (TLS In-Transit Encryption)
# ------------------------------------------------------------------------------
echo "[5/7] Генерация сертификата для Redis..."
openssl genrsa -out redis.key 2048
chmod 600 redis.key

cat > redis_ext.cnf << 'EOF'
[req]
default_bits = 2048
prompt = no
default_md = sha256
req_extensions = req_ext
distinguished_name = dn

[dn]
C = RU
O = FinancialAI
OU = In-Memory
CN = redis

[req_ext]
subjectAltName = @alt_names

[alt_names]
DNS.1 = redis
DNS.2 = redis-server
DNS.3 = localhost
IP.1 = 127.0.0.1
EOF

openssl req -new -key redis.key -out redis.csr -config redis_ext.cnf

cat > redis_v3.ext << 'EOF'
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth, clientAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = redis
DNS.2 = redis-server
DNS.3 = localhost
IP.1 = 127.0.0.1
EOF

openssl x509 -req -in redis.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -out redis.crt -days "${DAYS_CERT}" -sha256 -extfile redis_v3.ext

chmod 600 redis.key
chmod 644 redis.crt
rm -f redis.csr redis_ext.cnf redis_v3.ext
echo "  -> Успешно создан redis.crt и redis.key"

# ------------------------------------------------------------------------------
# 6. Генерация сертификата для MinIO (S3 TLS In-Transit Encryption)
# ------------------------------------------------------------------------------
echo "[6/7] Генерация сертификата для MinIO..."
openssl genrsa -out minio.key 2048
chmod 600 minio.key

cat > minio_ext.cnf << 'EOF'
[req]
default_bits = 2048
prompt = no
default_md = sha256
req_extensions = req_ext
distinguished_name = dn

[dn]
C = RU
O = FinancialAI
OU = Storage
CN = minio

[req_ext]
subjectAltName = @alt_names

[alt_names]
DNS.1 = minio
DNS.2 = minio-server
DNS.3 = localhost
IP.1 = 127.0.0.1
EOF

openssl req -new -key minio.key -out minio.csr -config minio_ext.cnf

cat > minio_v3.ext << 'EOF'
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = minio
DNS.2 = minio-server
DNS.3 = localhost
IP.1 = 127.0.0.1
EOF

openssl x509 -req -in minio.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -out minio.crt -days "${DAYS_CERT}" -sha256 -extfile minio_v3.ext

chmod 600 minio.key
chmod 644 minio.crt
rm -f minio.csr minio_ext.cnf minio_v3.ext

# Подготовка структуры каталога MinIO (/root/.minio/certs)
mkdir -p minio/CAs
cp minio.crt minio/public.crt
cp minio.key minio/private.key
cp ca.crt minio/CAs/ca.crt
chmod 600 minio/private.key
echo "  -> Успешно создан minio.crt, minio.key и структура каталога certs/minio/"

# ------------------------------------------------------------------------------
# 7. Проверка и отображение контрольных отпечатков (Fingerprints)
# ------------------------------------------------------------------------------
echo "[7/7] Сводка сгенерированных сертификатов Zero-Trust:"
echo "-----------------------------------------------------------------"
echo "Root CA Fingerprint (SHA256):"
openssl x509 -noout -fingerprint -sha256 -in ca.crt
echo ""
echo "Server Certificate Subject & SAN (Caddy):"
openssl x509 -noout -subject -ext subjectAltName -in server.crt
echo ""
echo "PostgreSQL Certificate Subject & SAN:"
openssl x509 -noout -subject -ext subjectAltName -in postgres.crt
echo ""
echo "Redis Certificate Subject & SAN:"
openssl x509 -noout -subject -ext subjectAltName -in redis.crt
echo ""
echo "MinIO Certificate Subject & SAN:"
openssl x509 -noout -subject -ext subjectAltName -in minio.crt
echo ""
echo "Client Certificate Subject & Serial (mTLS):"
openssl x509 -noout -subject -serial -in client.crt
echo "Client Certificate Fingerprint (SHA256):"
openssl x509 -noout -fingerprint -sha256 -in client.crt
echo "-----------------------------------------------------------------"
echo ""
echo "Готово! Все сертификаты сохранены в директории: ${CERTS_DIR}"
echo "Для включения строгой среды нулевого доверия укажите в .env файле:"
echo "  REQUIRE_MTLS=true"
echo "  ALLOWED_MTLS_SUBJECTS=admin-client"
echo "  CADDY_CLIENT_AUTH_MODE=require"
echo "  POSTGRES_SSLMODE=require"
echo "  REDIS_SSL=true"
echo "  MINIO_SECURE=true"
echo "================================================================="
