import os
import hmac
import hashlib
import json
from decimal import Decimal
# pyrefly: ignore [missing-import]
from fastapi import FastAPI, HTTPException, Security, Depends
# pyrefly: ignore [missing-import]
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel, field_validator

# Создаем приложение FastAPI
app = FastAPI(
    title="Digital Ruble Enclave Signer",
    description="Микросервис для криптографического подписания транзакций цифрового рубля внутри изолированного анклава Gramine (TEE).",
    version="1.0.0"
)

# Секретный мастер-ключ Центробанка (заглушка для хакатона).
# В реальном TEE этот ключ пробрасывается через механизм Secret Provisioning (RA-TLS).
MASTER_KEY_STR = os.getenv("ENCLAVE_MASTER_KEY", "")
SECRET_MASTER_KEY = MASTER_KEY_STR.encode('utf-8')

# Простой API ключ для базовой защиты эндпоинта
API_KEY = "test_api_key_for_digital_ruble"
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def get_api_key(api_key_header: str = Security(api_key_header)):
    if api_key_header == API_KEY:
        return api_key_header
    raise HTTPException(status_code=403, detail="Could not validate credentials")

class TransactionRequest(BaseModel):
    """Модель данных для входящего запроса на подпись смарт-контракта."""
    sender_wallet_id: str
    receiver_wallet_id: str
    amount: Decimal
    smart_contract_id: str | None = None

    @field_validator('amount')
    @classmethod
    def round_amount(cls, v: Decimal) -> Decimal:
        return v.quantize(Decimal('0.01'))

class SignatureResponse(BaseModel):
    """Модель ответа с криптографической подписью."""
    signature: str
    status: str = "success"

@app.post("/sign", response_model=SignatureResponse)
async def sign_transaction(req: TransactionRequest, api_key: str = Depends(get_api_key)):
    """
    Эндпоинт для подписания транзакции.
    Берет данные транзакции, сериализует их в предсказуемый JSON
    и создает HMAC SHA-256 подпись с использованием Секретного Ключа.
    """
    if not SECRET_MASTER_KEY:
        raise HTTPException(status_code=500, detail="Master key not loaded")

    try:
        # Сериализуем данные словаря в строку с сортировкой ключей, 
        # чтобы гарантировать одинаковый хэш для одинаковых данных.
        # Decimal преобразуется в строку для сохранения точности
        data_dict = req.model_dump()
        data_dict['amount'] = str(data_dict['amount'])
        data_str = json.dumps(data_dict, sort_keys=True)
        
        # Генерируем HMAC SHA256
        signature = hmac.new(
            SECRET_MASTER_KEY, 
            data_str.encode('utf-8'), 
            hashlib.sha256
        ).hexdigest()
        
        return SignatureResponse(signature=signature)
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка генерации подписи: {str(e)}")

@app.get("/health")
async def health():
    """Эндпоинт проверки жизнеспособности анклава."""
    if not SECRET_MASTER_KEY:
        raise HTTPException(status_code=503, detail="Enclave not provisioned with master key")
    return {"status": "ok", "enclave": "active", "tls": "enabled"}

if __name__ == "__main__":
    import uvicorn
    # Для HTTPS нам понадобятся сертификаты (создадутся в Dockerfile через openssl)
    uvicorn.run(
        "signer:app", 
        host="0.0.0.0", 
        port=8080, 
        ssl_keyfile="/certs/key.pem", 
        ssl_certfile="/certs/cert.pem"
    )
