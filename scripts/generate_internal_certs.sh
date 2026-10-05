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
# 4. Проверка и отображение контрольных отпечатков (Fingerprints)
# ------------------------------------------------------------------------------
echo "[4/4] Сводка сгенерированных сертификатов:"
echo "-----------------------------------------------------------------"
echo "Root CA Fingerprint (SHA256):"
openssl x509 -noout -fingerprint -sha256 -in ca.crt
echo ""
echo "Server Certificate Subject & SAN:"
openssl x509 -noout -subject -ext subjectAltName -in server.crt
echo ""
echo "Client Certificate Subject & Serial:"
openssl x509 -noout -subject -serial -in client.crt
echo "Client Certificate Fingerprint (SHA256):"
openssl x509 -noout -fingerprint -sha256 -in client.crt
echo "-----------------------------------------------------------------"
echo ""
echo "Готово! Все сертификаты сохранены в директории: ${CERTS_DIR}"
echo "Для включения строгой взаимной аутентификации укажите в .env файле:"
echo "  REQUIRE_MTLS=true"
echo "  ALLOWED_MTLS_SUBJECTS=admin-client"
echo "  CADDY_CLIENT_AUTH_MODE=require"
echo "================================================================="
