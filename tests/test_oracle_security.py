import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.simulations.api.routers.ruble import router as ruble_router


@pytest.fixture(autouse=True)
def setup_tokens(monkeypatch):
    monkeypatch.setenv("AGENT_BANK_BOOTSTRAP_TOKEN", "bank_secret_token")
    monkeypatch.setenv("AGENT_INVEST_BOOTSTRAP_TOKEN", "invest_secret_token")
    monkeypatch.setenv("AGENT_DIGITAL_BOOTSTRAP_TOKEN", "digital_secret_token")
    monkeypatch.setenv("AGENT_MAIN_BOOTSTRAP_TOKEN", "orchestrator_secret_token")
    monkeypatch.setenv("ORACLE_BOOTSTRAP_TOKEN", "trusted_oracle_token_999")
    monkeypatch.setenv("REQUIRE_ORACLE_SIGNATURE", "false")
    monkeypatch.setenv("REQUIRE_ENCLAVE_SIGNATURE", "false")


class DummySmartContract:
    def __init__(self, id="c_1", status="active", condition_status="pending"):
        self.id = id
        self.status = status
        self.condition_status = condition_status


def create_mock_db_context(contract):
    """Создает мок контекстного менеджера `async with ruble_db() as db:`."""
    session = AsyncMock()
    session.scalar.return_value = contract
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    cm = MagicMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = None
    return cm


def test_oracle_endpoint_requires_auth():
    app = FastAPI()
    app.include_router(ruble_router, prefix="/ruble")
    client = TestClient(app)

    response = client.post(
        "/ruble/smart-contracts/c_1/condition",
        json={"status": "fulfilled"}
    )
    assert response.status_code == 401
    assert "Отсутствует токен аутентификации" in response.json()["detail"]


def test_oracle_endpoint_rejects_foreign_agent_tokens():
    app = FastAPI()
    app.include_router(ruble_router, prefix="/ruble")
    client = TestClient(app)

    # Токен банка отклоняется с 403
    headers_bank = {"X-Bootstrap-Token": "bank_secret_token"}
    res_bank = client.post(
        "/ruble/smart-contracts/c_1/condition",
        json={"status": "fulfilled"},
        headers=headers_bank
    )
    assert res_bank.status_code == 403
    assert "запрещен для данного агента" in res_bank.json()["detail"]

    # Токен инвестиций отклоняется с 403
    headers_invest = {"X-Bootstrap-Token": "invest_secret_token"}
    res_invest = client.post(
        "/ruble/smart-contracts/c_1/condition",
        json={"status": "fulfilled"},
        headers=headers_invest
    )
    assert res_invest.status_code == 403


def test_oracle_token_cannot_access_other_ruble_endpoints():
    app = FastAPI()
    app.include_router(ruble_router, prefix="/ruble")
    client = TestClient(app)

    # Оракул пытается прочитать кошелек клиента
    headers_oracle = {"X-Bootstrap-Token": "trusted_oracle_token_999"}
    res_wallet = client.get("/ruble/wallets/client_100", headers=headers_oracle)
    assert res_wallet.status_code == 403
    assert "запрещен для данного агента" in res_wallet.json()["detail"]


def test_oracle_update_success_with_oracle_token(monkeypatch):
    contract = DummySmartContract(id="c_123", status="active", condition_status="pending")
    mock_cm = create_mock_db_context(contract)
    monkeypatch.setattr("src.simulations.api.routers.ruble.ruble_db", lambda: mock_cm)

    app = FastAPI()
    app.include_router(ruble_router, prefix="/ruble")
    client = TestClient(app)

    headers = {"X-Bootstrap-Token": "trusted_oracle_token_999"}
    payload = {
        "status": "fulfilled",
        "oracle_name": "logistics_tracker",
        "reason": "Товар доставлен покупателю"
    }

    response = client.post("/ruble/smart-contracts/c_123/condition", json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["new_condition_status"] == "fulfilled"
    assert response.json()["oracle_name"] == "logistics_tracker"
    assert contract.condition_status == "fulfilled"


def test_oracle_update_success_with_digital_agent_token(monkeypatch):
    contract = DummySmartContract(id="c_123", status="active", condition_status="pending")
    mock_cm = create_mock_db_context(contract)
    monkeypatch.setattr("src.simulations.api.routers.ruble.ruble_db", lambda: mock_cm)

    app = FastAPI()
    app.include_router(ruble_router, prefix="/ruble")
    client = TestClient(app)

    headers = {"X-Bootstrap-Token": "digital_secret_token"}
    payload = {"status": "failed", "reason": "Срок поставки истек"}

    response = client.post("/ruble/smart-contracts/c_123/condition", json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json()["new_condition_status"] == "failed"
    assert contract.condition_status == "failed"


def test_oracle_update_success_with_orchestrator_token(monkeypatch):
    contract = DummySmartContract(id="c_123", status="active", condition_status="pending")
    mock_cm = create_mock_db_context(contract)
    monkeypatch.setattr("src.simulations.api.routers.ruble.ruble_db", lambda: mock_cm)

    app = FastAPI()
    app.include_router(ruble_router, prefix="/ruble")
    client = TestClient(app)

    headers = {"X-Bootstrap-Token": "orchestrator_secret_token"}
    payload = {"status": "fulfilled"}

    response = client.post("/ruble/smart-contracts/c_123/condition", json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json()["new_condition_status"] == "fulfilled"


def test_oracle_rejects_invalid_status_value():
    app = FastAPI()
    app.include_router(ruble_router, prefix="/ruble")
    client = TestClient(app)

    headers = {"X-Bootstrap-Token": "trusted_oracle_token_999"}
    payload = {"status": "random_fake_status"}

    response = client.post("/ruble/smart-contracts/c_123/condition", json=payload, headers=headers)
    assert response.status_code == 400
    assert "Разрешены только 'fulfilled' или 'failed'" in response.json()["detail"]


def test_oracle_rejects_already_fulfilled_condition(monkeypatch):
    contract = DummySmartContract(id="c_123", status="active", condition_status="fulfilled")
    mock_cm = create_mock_db_context(contract)
    monkeypatch.setattr("src.simulations.api.routers.ruble.ruble_db", lambda: mock_cm)

    app = FastAPI()
    app.include_router(ruble_router, prefix="/ruble")
    client = TestClient(app)

    headers = {"X-Bootstrap-Token": "trusted_oracle_token_999"}
    payload = {"status": "failed", "reason": "Передумали"}

    response = client.post("/ruble/smart-contracts/c_123/condition", json=payload, headers=headers)
    assert response.status_code == 409
    assert "уже зафиксировано" in response.json()["detail"]


def test_oracle_rejects_inactive_contract(monkeypatch):
    contract = DummySmartContract(id="c_123", status="cancelled", condition_status="pending")
    mock_cm = create_mock_db_context(contract)
    monkeypatch.setattr("src.simulations.api.routers.ruble.ruble_db", lambda: mock_cm)

    app = FastAPI()
    app.include_router(ruble_router, prefix="/ruble")
    client = TestClient(app)

    headers = {"X-Bootstrap-Token": "trusted_oracle_token_999"}
    payload = {"status": "fulfilled"}

    response = client.post("/ruble/smart-contracts/c_123/condition", json=payload, headers=headers)
    assert response.status_code == 400
    assert "Контракт не активен" in response.json()["detail"]


def test_oracle_rejects_missing_contract(monkeypatch):
    mock_cm = create_mock_db_context(None)
    monkeypatch.setattr("src.simulations.api.routers.ruble.ruble_db", lambda: mock_cm)

    app = FastAPI()
    app.include_router(ruble_router, prefix="/ruble")
    client = TestClient(app)

    headers = {"X-Bootstrap-Token": "trusted_oracle_token_999"}
    payload = {"status": "fulfilled"}

    response = client.post("/ruble/smart-contracts/non_existent/condition", json=payload, headers=headers)
    assert response.status_code == 404
    assert "не найден" in response.json()["detail"]


def test_oracle_endpoint_strict_requires_signature_by_default(monkeypatch):
    """Проверяет, что по умолчанию (SEC-08) без подписи оракула возвращается HTTP 401."""
    monkeypatch.delenv("REQUIRE_ORACLE_SIGNATURE", raising=False)
    contract = DummySmartContract(id="c_strict", status="active", condition_status="pending")
    mock_cm = create_mock_db_context(contract)
    monkeypatch.setattr("src.simulations.api.routers.ruble.ruble_db", lambda: mock_cm)

    app = FastAPI()
    app.include_router(ruble_router, prefix="/ruble")
    client = TestClient(app)

    headers = {"X-Bootstrap-Token": "trusted_oracle_token_999"}
    payload = {"status": "fulfilled"}

    # Без подписи оракула запрос отклоняется с 401
    resp = client.post("/ruble/smart-contracts/c_strict/condition", json=payload, headers=headers)
    assert resp.status_code == 401
    assert "Требуется цифровая подпись оракула" in resp.json()["detail"]


def test_oracle_endpoint_valid_signature_with_nonce_and_replay_protection(monkeypatch):
    """Проверяет успешное обновление с подписью оракула и защиту от Replay-атак (SEC-14)."""
    import time
    from src.simulations.core.crypto.oracle_verifier import get_oracle_verifier

    monkeypatch.setenv("REQUIRE_ORACLE_SIGNATURE", "true")
    contract = DummySmartContract(id="c_replay", status="active", condition_status="pending")
    mock_cm = create_mock_db_context(contract)
    monkeypatch.setattr("src.simulations.api.routers.ruble.ruble_db", lambda: mock_cm)

    app = FastAPI()
    app.include_router(ruble_router, prefix="/ruble")
    client = TestClient(app)

    headers = {"X-Bootstrap-Token": "trusted_oracle_token_999"}
    verifier = get_oracle_verifier()
    now_ts = int(time.time())
    nonce = f"test-replay-nonce-{now_ts}"

    sig = verifier.sign_condition(
        contract_id="c_replay",
        status="fulfilled",
        oracle_name="default_oracle",
        nonce=nonce,
        timestamp=now_ts,
    )

    payload = {
        "status": "fulfilled",
        "oracle_name": "default_oracle",
        "signature": sig,
        "nonce": nonce,
        "timestamp": now_ts,
    }

    # Первый запрос успешен (200)
    resp1 = client.post("/ruble/smart-contracts/c_replay/condition", json=payload, headers=headers)
    assert resp1.status_code == 200

    # Второй запрос с тем же nonce отклоняется с 409 (Replay Attack)
    resp2 = client.post("/ruble/smart-contracts/c_replay/condition", json=payload, headers=headers)
    assert resp2.status_code == 409
    assert "Replay-атака" in resp2.json()["detail"] or "уже был использован" in resp2.json()["detail"]
