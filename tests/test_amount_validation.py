import pytest
from decimal import Decimal
from pydantic import ValidationError
from fastapi.testclient import TestClient

from src.simulations.api.routers.bank import TransferRequest, AutoPaymentCreate
from src.simulations.api.routers.invest import OpenProductRequest
from src.simulations.api.routers.ruble import (
    FundWithdrawRequest,
    RubleTransferRequest,
    CreateContractRequest,
)
from src.simulations.api.main import app


# --- Тестирование Pydantic-схем банка ---

def test_bank_transfer_request_amount_validation():
    # Отрицательная сумма
    with pytest.raises(ValidationError):
        TransferRequest(
            from_account_id="acc_1",
            amount=Decimal("-100.00"),
            transfer_type="account",
            destination="acc_2"
        )

    # Нулевая сумма
    with pytest.raises(ValidationError):
        TransferRequest(
            from_account_id="acc_1",
            amount=Decimal("0.00"),
            transfer_type="account",
            destination="acc_2"
        )

    # Корректная сумма
    req = TransferRequest(
        from_account_id="acc_1",
        amount=Decimal("150.50"),
        transfer_type="account",
        destination="acc_2"
    )
    assert req.amount == Decimal("150.50")


def test_bank_autopayment_create_amount_validation():
    # Отрицательная сумма
    with pytest.raises(ValidationError):
        AutoPaymentCreate(
            amount=Decimal("-50.00"),
            recipient="org_1",
            schedule="monthly",
            next_payment_date="2026-10-15"
        )

    # Нулевая сумма
    with pytest.raises(ValidationError):
        AutoPaymentCreate(
            amount=Decimal("0.00"),
            recipient="org_1",
            schedule="monthly",
            next_payment_date="2026-10-15"
        )

    # Корректная сумма
    ap = AutoPaymentCreate(
        amount=Decimal("500.00"),
        recipient="org_1",
        schedule="monthly",
        next_payment_date="2026-10-15"
    )
    assert ap.amount == Decimal("500.00")


# --- Тестирование Pydantic-схем инвестиций ---

def test_invest_open_product_amount_and_term_validation():
    # Отрицательная сумма
    with pytest.raises(ValidationError):
        OpenProductRequest(
            client_id="cl_1",
            from_bank_account_id="acc_1",
            initial_amount=Decimal("-1000.00"),
            term_months=12
        )

    # Нулевая сумма
    with pytest.raises(ValidationError):
        OpenProductRequest(
            client_id="cl_1",
            from_bank_account_id="acc_1",
            initial_amount=Decimal("0.00"),
            term_months=12
        )

    # Отрицательный или нулевой срок вклада
    with pytest.raises(ValidationError):
        OpenProductRequest(
            client_id="cl_1",
            from_bank_account_id="acc_1",
            initial_amount=Decimal("1000.00"),
            term_months=0
        )

    # Корректный вклад
    req = OpenProductRequest(
        client_id="cl_1",
        from_bank_account_id="acc_1",
        initial_amount=Decimal("25000.00"),
        term_months=6
    )
    assert req.initial_amount == Decimal("25000.00")
    assert req.term_months == 6


# --- Тестирование Pydantic-схем цифрового рубля ---

def test_ruble_fund_withdraw_amount_validation():
    # Отрицательная сумма
    with pytest.raises(ValidationError):
        FundWithdrawRequest(bank_account_id="acc_1", amount=Decimal("-10.00"))

    # Нулевая сумма
    with pytest.raises(ValidationError):
        FundWithdrawRequest(bank_account_id="acc_1", amount=Decimal("0.00"))

    # Корректная сумма
    fw = FundWithdrawRequest(bank_account_id="acc_1", amount=Decimal("500.00"))
    assert fw.amount == Decimal("500.00")


def test_ruble_transfer_request_amount_validation():
    # Отрицательная сумма
    with pytest.raises(ValidationError):
        RubleTransferRequest(to_wallet_id="w_2", amount=Decimal("-25.00"))

    # Нулевая сумма
    with pytest.raises(ValidationError):
        RubleTransferRequest(to_wallet_id="w_2", amount=Decimal("0.00"))

    # Корректная сумма
    rt = RubleTransferRequest(to_wallet_id="w_2", amount=Decimal("100.00"))
    assert rt.amount == Decimal("100.00")


def test_ruble_create_contract_amount_validation():
    # Отрицательная сумма
    with pytest.raises(ValidationError):
        CreateContractRequest(
            receiver_wallet_id="w_2",
            amount=Decimal("-1.00"),
            condition_type="delivery",
            contract_code="def execute(ctx): pass"
        )

    # Нулевая сумма
    with pytest.raises(ValidationError):
        CreateContractRequest(
            receiver_wallet_id="w_2",
            amount=Decimal("0.00"),
            condition_type="delivery",
            contract_code="def execute(ctx): pass"
        )

    # Корректный контракт
    cc = CreateContractRequest(
        receiver_wallet_id="w_2",
        amount=Decimal("5000.00"),
        condition_type="delivery",
        contract_code="def execute(ctx): pass"
    )
    assert cc.amount == Decimal("5000.00")


# --- Тестирование отклонения через HTTP API (HTTP 422) ---

def test_http_endpoints_reject_non_positive_amounts(monkeypatch):
    monkeypatch.setenv("AGENT_BANK_BOOTSTRAP_TOKEN", "bank_tok")
    monkeypatch.setenv("AGENT_INVEST_BOOTSTRAP_TOKEN", "invest_tok")
    monkeypatch.setenv("AGENT_DIGITAL_BOOTSTRAP_TOKEN", "ruble_tok")

    client = TestClient(app)

    # 1. Банковский перевод с отрицательной суммой
    res_bank = client.post(
        "/bank/transfers",
        json={
            "from_account_id": "acc_1",
            "amount": -50.0,
            "transfer_type": "account",
            "destination": "acc_2"
        },
        headers={"X-Bootstrap-Token": "bank_tok"}
    )
    assert res_bank.status_code == 422

    # 2. Открытие вклада с нулевой суммой
    res_invest = client.post(
        "/invest/deposits/open",
        json={
            "client_id": "cl_1",
            "from_bank_account_id": "acc_1",
            "initial_amount": 0.0,
            "term_months": 12
        },
        headers={"X-Bootstrap-Token": "invest_tok"}
    )
    assert res_invest.status_code == 422

    # 3. P2P перевод цифрового рубля с отрицательной суммой
    res_ruble = client.post(
        "/ruble/wallets/w_1/transfers",
        json={
            "to_wallet_id": "w_2",
            "amount": -100.0
        },
        headers={"X-Bootstrap-Token": "ruble_tok"}
    )
    assert res_ruble.status_code == 422
