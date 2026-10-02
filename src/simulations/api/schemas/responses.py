from datetime import date, time, datetime
from decimal import Decimal
from typing import Optional, List
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
    id: Optional[str] = None
    name: Optional[str] = None
    service_cost: Optional[Decimal] = Field(default=Decimal("0.00"))
    non_sbp_limit: Optional[Decimal] = Field(default=Decimal("0.00"))
    sbp_limit: Optional[Decimal] = Field(default=Decimal("30000000.00"))
    cash_withdrawal_limit: Optional[Decimal] = Field(default=Decimal("0.00"))
    cash_withdrawal_fee: Optional[Decimal] = Field(default=Decimal("100.00"))
    transfer_over_limit_fee_percent: Optional[Decimal] = Field(default=Decimal("2.00"))
    deposit_fee: Optional[Decimal] = Field(default=Decimal("0.00"))


class AccountResponse(BaseResponseModel):
    id: Optional[str] = None
    client_id: Optional[str] = None
    account_number: Optional[str] = None
    balance: Optional[Decimal] = Field(default=Decimal("0.00"))
    currency: Optional[str] = "RUB"
    tariff_id: Optional[str] = None
    status: Optional[str] = "active"
    created_at: Optional[datetime] = None


class CardResponse(BaseResponseModel):
    id: Optional[str] = None
    account_id: Optional[str] = None
    card_number: Optional[str] = None
    status: Optional[str] = "active"
    created_at: Optional[datetime] = None


class TransactionResponse(BaseResponseModel):
    id: Optional[str] = None
    account_id: Optional[str] = None
    category: Optional[str] = None
    operation_type: Optional[str] = None
    amount: Optional[Decimal] = Field(default=Decimal("0.00"))
    commission: Optional[Decimal] = Field(default=Decimal("0.00"))
    description: Optional[str] = None
    status: Optional[str] = "completed"
    date: Optional[date] = None
    time: Optional[time] = None


class AutoPaymentResponse(BaseResponseModel):
    id: Optional[str] = None
    account_id: Optional[str] = None
    amount: Optional[Decimal] = Field(default=Decimal("0.00"))
    recipient: Optional[str] = None
    schedule: Optional[str] = None
    next_payment_date: Optional[date] = None
    is_active: Optional[bool] = True


# ============================================================================
# СХЕМЫ ИНВЕСТИЦИОННЫХ ОТВЕТОВ
# ============================================================================

class InvestmentStrategyResponse(BaseResponseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    expected_yield: Optional[Decimal] = Field(default=Decimal("0.00"))
    risk_level: Optional[str] = None
    instruments: Optional[str] = None
    commission_fee: Optional[Decimal] = Field(default=Decimal("0.00"))


class SavingsAccountResponse(BaseResponseModel):
    id: Optional[str] = None
    client_id: Optional[str] = None
    account_number: Optional[str] = None
    balance: Optional[Decimal] = Field(default=Decimal("0.00"))
    interest_rate: Optional[Decimal] = Field(default=Decimal("0.00"))
    status: Optional[str] = "active"
    next_payment_date: Optional[datetime] = None
    next_payment_amount: Optional[Decimal] = Field(default=Decimal("0.00"))
    opened_at: Optional[datetime] = None


class DepositResponse(BaseResponseModel):
    id: Optional[str] = None
    client_id: Optional[str] = None
    account_number: Optional[str] = None
    balance: Optional[Decimal] = Field(default=Decimal("0.00"))
    interest_rate: Optional[Decimal] = Field(default=Decimal("0.00"))
    term_months: Optional[int] = 12
    status: Optional[str] = "active"
    next_payment_date: Optional[datetime] = None
    next_payment_amount: Optional[Decimal] = Field(default=Decimal("0.00"))
    opened_at: Optional[datetime] = None


class BrokerAccountResponse(BaseResponseModel):
    id: Optional[str] = None
    client_id: Optional[str] = None
    account_number: Optional[str] = None
    balance: Optional[Decimal] = Field(default=Decimal("0.00"))
    monthly_income: Optional[Decimal] = Field(default=Decimal("0.00"))
    strategy_id: Optional[str] = None
    status: Optional[str] = "active"
    next_commission_date: Optional[datetime] = None
    opened_at: Optional[datetime] = None


class PortfolioResponse(BaseResponseModel):
    savings_accounts: List[SavingsAccountResponse] = Field(default_factory=list)
    deposits: List[DepositResponse] = Field(default_factory=list)
    broker_accounts: List[BrokerAccountResponse] = Field(default_factory=list)


# ============================================================================
# СХЕМЫ ОТВЕТОВ ЦИФРОВОГО РУБЛЯ
# ============================================================================

class WalletResponse(BaseResponseModel):
    id: Optional[str] = None
    client_id: Optional[str] = None
    wallet_number: Optional[str] = None
    balance: Optional[Decimal] = Field(default=Decimal("0.00"))
    frozen_balance: Optional[Decimal] = Field(default=Decimal("0.00"))
    status: Optional[str] = "active"
    opened_at: Optional[datetime] = None


class RubleTransactionResponse(BaseResponseModel):
    id: Optional[str] = None
    sender_wallet_id: Optional[str] = None
    receiver_wallet_id: Optional[str] = None
    amount: Optional[Decimal] = Field(default=Decimal("0.00"))
    status: Optional[str] = "completed"
    smart_contract_id: Optional[str] = None
    signature: Optional[str] = None
    timestamp: Optional[datetime] = None


class SmartContractResponse(BaseResponseModel):
    id: Optional[str] = None
    creator_wallet_id: Optional[str] = None
    receiver_wallet_id: Optional[str] = None
    amount: Optional[Decimal] = Field(default=Decimal("0.00"))
    condition_type: Optional[str] = None
    contract_code: Optional[str] = None
    condition_status: Optional[str] = "pending"
    status: Optional[str] = "active"
    error_message: Optional[str] = None
    created_at: Optional[datetime] = None
    executed_at: Optional[datetime] = None
