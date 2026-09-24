import requests
import urllib3
from decimal import Decimal

# Отключаем предупреждения InsecureRequestWarning для самоподписанных сертификатов (при запуске теста с хоста)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ENCLAVE_URL = "https://localhost:8080"
API_KEY = "test_api_key_for_digital_ruble"

def test_enclave_health():
    """Проверка жизнеспособности анклава."""
    res = requests.get(f"{ENCLAVE_URL}/health", verify=False)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["enclave"] == "active"

def test_enclave_signature():
    """Запрос криптографической подписи у анклава TEE."""
    payload = {
        "sender_wallet_id": "11111111-1111-1111-1111-111111111111",
        "receiver_wallet_id": "22222222-2222-2222-2222-222222222222",
        "amount": 100.50,
        "smart_contract_id": "33333333-3333-3333-3333-333333333333"
    }
    headers = {"X-API-Key": API_KEY}
    
    res = requests.post(f"{ENCLAVE_URL}/sign", json=payload, headers=headers, verify=False)
    assert res.status_code == 200
    
    data = res.json()
    assert "signature" in data
    assert len(data["signature"]) == 64  # HMAC SHA256 is 64 hex characters
    assert data["status"] == "success"

def test_enclave_signature_unauthorized():
    """Запрос без API ключа должен быть отклонен."""
    payload = {
        "sender_wallet_id": "11111111-1111-1111-1111-111111111111",
        "receiver_wallet_id": "22222222-2222-2222-2222-222222222222",
        "amount": 100.50
    }
    res = requests.post(f"{ENCLAVE_URL}/sign", json=payload, verify=False)
    assert res.status_code == 403

def test_enclave_deterministic_signature():
    """Проверка детерминированности подписи (float issues fixes)."""
    payload1 = {
        "sender_wallet_id": "11111111-1111-1111-1111-111111111111",
        "receiver_wallet_id": "22222222-2222-2222-2222-222222222222",
        "amount": 100.10,
        "smart_contract_id": "33333333-3333-3333-3333-333333333333"
    }
    # Тот же payload, но сумма строкой (pydantic должен распарсить в тот же Decimal)
    payload2 = {
        "sender_wallet_id": "11111111-1111-1111-1111-111111111111",
        "receiver_wallet_id": "22222222-2222-2222-2222-222222222222",
        "amount": "100.10",
        "smart_contract_id": "33333333-3333-3333-3333-333333333333"
    }
    headers = {"X-API-Key": API_KEY}
    
    res1 = requests.post(f"{ENCLAVE_URL}/sign", json=payload1, headers=headers, verify=False)
    res2 = requests.post(f"{ENCLAVE_URL}/sign", json=payload2, headers=headers, verify=False)
    
    assert res1.status_code == 200
    assert res2.status_code == 200
    assert res1.json()["signature"] == res2.json()["signature"]
