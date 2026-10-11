import datetime as dt
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class BaseResponseModel(BaseModel):
    """
    Базовая схема ответа API с поддержкой сериализации из атрибутов ORM (from_attributes=True)
    и обратной совместимостью со словароподобным доступом (__getitem__, __contains__).
    """

    model_config = ConfigDict(from_attributes=True)

    def __getitem__(self, item: str):
        return getattr(self, item)

    def __contains__(self, item: str) -> bool:
        return hasattr(self, item)


# ============================================================================
# СХЕМЫ БАНКОВСКИХ ОТВЕТОВ
# ============================================================================


class TariffResponse(BaseResponseModel):
    id: str | None = None
    name: str | None = None
    service_cost: Decimal | None = Field(default=Decimal("0.00"))
    non_sbp_limit: Decimal | None = Field(default=Decimal("0.00"))
    sbp_limit: Decimal | None = Field(default=Decimal("30000000.00"))
    cash_withdrawal_limit: Decimal | None = Field(default=Decimal("0.00"))
    cash_withdrawal_fee: Decimal | None = Field(default=Decimal("100.00"))
    transfer_over_limit_fee_percent: Decimal | None = Field(default=Decimal("2.00"))
    deposit_fee: Decimal | None = Field(default=Decimal("0.00"))


class AccountResponse(BaseResponseModel):
    id: str | None = None
    client_id: str | None = None
    account_number: str | None = None
    balance: Decimal | None = Field(default=Decimal("0.00"))
    currency: str | None = "RUB"
    tariff_id: str | None = None
    status: str | None = "active"
    created_at: dt.datetime | None = None


class CardResponse(BaseResponseModel):
    id: str | None = None
    account_id: str | None = None
    card_number: str | None = None
    status: str | None = "active"
    created_at: dt.datetime | None = None


class TransactionResponse(BaseResponseModel):
    id: str | None = None
    account_id: str | None = None
    category: str | None = None
    operation_type: str | None = None
    amount: Decimal | None = Field(default=Decimal("0.00"))
    commission: Decimal | None = Field(default=Decimal("0.00"))
    description: str | None = None
    status: str | None = "completed"
    date: dt.date | None = None
    time: dt.time | None = None


class AutoPaymentResponse(BaseResponseModel):
    id: str | None = None
    account_id: str | None = None
    amount: Decimal | None = Field(default=Decimal("0.00"))
    recipient: str | None = None
    schedule: str | None = None
    next_payment_date: dt.date | None = None
    is_active: bool | None = True


# ============================================================================
# СХЕМЫ ИНВЕСТИЦИОННЫХ ОТВЕТОВ
# ============================================================================


class InvestmentStrategyResponse(BaseResponseModel):
    id: str | None = None
    name: str | None = None
    expected_yield: Decimal | None = Field(default=Decimal("0.00"))
    risk_level: str | None = None
    instruments: str | None = None
    commission_fee: Decimal | None = Field(default=Decimal("0.00"))


class SavingsAccountResponse(BaseResponseModel):
    id: str | None = None
    client_id: str | None = None
    account_number: str | None = None
    balance: Decimal | None = Field(default=Decimal("0.00"))
    interest_rate: Decimal | None = Field(default=Decimal("0.00"))
    status: str | None = "active"
    next_payment_date: dt.datetime | None = None
    next_payment_amount: Decimal | None = Field(default=Decimal("0.00"))
    opened_at: dt.datetime | None = None


class DepositResponse(BaseResponseModel):
    id: str | None = None
    client_id: str | None = None
    account_number: str | None = None
    balance: Decimal | None = Field(default=Decimal("0.00"))
    interest_rate: Decimal | None = Field(default=Decimal("0.00"))
    term_months: int | None = 12
    status: str | None = "active"
    next_payment_date: dt.datetime | None = None
    next_payment_amount: Decimal | None = Field(default=Decimal("0.00"))
    opened_at: dt.datetime | None = None


class BrokerAccountResponse(BaseResponseModel):
    id: str | None = None
    client_id: str | None = None
    account_number: str | None = None
    balance: Decimal | None = Field(default=Decimal("0.00"))
    monthly_income: Decimal | None = Field(default=Decimal("0.00"))
    strategy_id: str | None = None
    status: str | None = "active"
    next_commission_date: dt.datetime | None = None
    opened_at: dt.datetime | None = None


class PortfolioResponse(BaseResponseModel):
    savings_accounts: list[SavingsAccountResponse] = Field(default_factory=list)
    deposits: list[DepositResponse] = Field(default_factory=list)
    broker_accounts: list[BrokerAccountResponse] = Field(default_factory=list)


# ============================================================================
# СХЕМЫ ОТВЕТОВ ЦИФРОВОГО РУБЛЯ
# ============================================================================


class WalletResponse(BaseResponseModel):
    id: str | None = None
    client_id: str | None = None
    wallet_number: str | None = None
    balance: Decimal | None = Field(default=Decimal("0.00"))
    frozen_balance: Decimal | None = Field(default=Decimal("0.00"))
    status: str | None = "active"
    opened_at: dt.datetime | None = None


class RubleTransactionResponse(BaseResponseModel):
    id: str | None = None
    sender_wallet_id: str | None = None
    receiver_wallet_id: str | None = None
    amount: Decimal | None = Field(default=Decimal("0.00"))
    status: str | None = "completed"
    smart_contract_id: str | None = None
    signature: str | None = None
    timestamp: dt.datetime | None = None


class SmartContractResponse(BaseResponseModel):
    id: str | None = None
    creator_wallet_id: str | None = None
    receiver_wallet_id: str | None = None
    amount: Decimal | None = Field(default=Decimal("0.00"))
    condition_type: str | None = None
    contract_code: str | None = None
    condition_status: str | None = "pending"
    status: str | None = "active"
    error_message: str | None = None
    created_at: dt.datetime | None = None
    executed_at: dt.datetime | None = None


# ============================================================================
# СХЕМЫ КРИПТОГРАФИЧЕСКИХ ЧЕКОВ И СЕРТИФИКАТОВ (БЛОК 5 АРХИТЕКТУРЫ)
# ============================================================================


class SignedReceiptData(BaseResponseModel):
    receipt_id: str
    action: str
    status: str = "completed"
    timestamp: str
    request_nonce: str | None = None
    data: dict = Field(default_factory=dict)


class SignedReceiptResponse(BaseResponseModel):
    receipt: SignedReceiptData
    signature: str
    algorithm: str = "SHA256withRSA-PSS"
    signer_cn: str = "Financial AI Simulation Bank"
    cert_fingerprint: str
    bank_cert_pem: str
    ca_cert_pem: str | None = None


class TransferSuccessResponse(BaseResponseModel):
    status: str = "success"
    transaction_id: str | None = None
    signed_receipt: SignedReceiptResponse | None = None


class BankCertificatesInfoResponse(BaseResponseModel):
    root_ca_pem: str
    bank_cert_pem: str
    bank_fingerprint: str
    signer_cn: str
