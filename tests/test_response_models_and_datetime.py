import inspect
from datetime import datetime, timezone, timedelta, date, time
from decimal import Decimal
import pytest

from src.simulations.db.bank.db.models import (
    get_moscow_now as bank_moscow_now,
    Account as BankAccount,
    Card as BankCard,
    Transaction as BankTransaction,
    Tariff as BankTariff,
    AutoPayment as BankAutoPayment,
)
from src.simulations.db.invest.db.models import (
    get_moscow_now as invest_moscow_now,
    SavingsAccount as InvestSavings,
    Deposit as InvestDeposit,
    BrokerAccount as InvestBroker,
    InvestmentStrategy as InvestStrategy,
)
from src.simulations.db.digital_ruble.db.models import (
    get_moscow_now as ruble_moscow_now,
    Wallet as RubleWallet,
    RubleTransaction,
    SmartContract as RubleContract,
)
from src.simulations.api.schemas.responses import (
    BaseResponseModel,
    AccountResponse,
    TariffResponse,
    CardResponse,
    TransactionResponse,
    AutoPaymentResponse,
    InvestmentStrategyResponse,
    SavingsAccountResponse,
    DepositResponse,
    BrokerAccountResponse,
    PortfolioResponse,
    WalletResponse,
    RubleTransactionResponse,
    SmartContractResponse,
)
from src.simulations.api.routers.bank import router as bank_router
from src.simulations.api.routers.invest import router as invest_router
from src.simulations.api.routers.ruble import router as ruble_router
from src.simulations.api.core.utils import to_dict, to_dict_list


def test_no_deprecated_utcnow_in_simulation_models():
    """
    Проверяет, что функции получения времени используют datetime.now(timezone.utc),
    а не устаревший datetime.utcnow().
    """
    for fn in (bank_moscow_now, invest_moscow_now, ruble_moscow_now):
        src = inspect.getsource(fn)
        assert "utcnow" not in src
        assert "timezone.utc" in src

        # Проверяем, что возвращаемое время соответствует Московскому времени (UTC+3)
        res = fn()
        assert isinstance(res, datetime)
        # Наше локальное или UTC время + 3 часа приблизительно равно res
        expected_approx = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(tzinfo=None)
        diff_seconds = abs((res - expected_approx).total_seconds())
        assert diff_seconds < 5


def test_base_response_model_dict_like_access():
    """
    Проверяет обратную совместимость BaseResponseModel со словароподобным доступом.
    """
    class TestModel(BaseResponseModel):
        id: str
        value: Decimal

    obj = TestModel(id="test_id", value=Decimal("123.45"))
    assert "id" in obj
    assert "value" in obj
    assert "non_existent" not in obj
    assert obj["id"] == "test_id"
    assert obj["value"] == Decimal("123.45")
    assert obj.id == "test_id"
    assert obj.value == Decimal("123.45")


def test_bank_response_models_validation():
    acc = BankAccount(id="acc_1", client_id="c_1", account_number="408178100", balance=Decimal("500.00"), status="active")
    acc_resp = AccountResponse.model_validate(acc)
    assert acc_resp.id == "acc_1"
    assert acc_resp.balance == Decimal("500.00")
    assert acc_resp.status == "active"

    card = BankCard(id="c_1", account_id="acc_1", card_number="42761234", status="active")
    card_resp = CardResponse.model_validate(card)
    assert card_resp.card_number == "42761234"

    tx = BankTransaction(id="tx_1", account_id="acc_1", category="expense", operation_type="purchase", amount=Decimal("100.00"))
    tx_resp = TransactionResponse.model_validate(tx)
    assert tx_resp.amount == Decimal("100.00")

    ap = BankAutoPayment(id="ap_1", account_id="acc_1", amount=Decimal("50.00"), recipient="phone", schedule="monthly", next_payment_date=date(2026, 11, 1))
    ap_resp = AutoPaymentResponse.model_validate(ap)
    assert ap_resp.schedule == "monthly"


def test_invest_response_models_validation():
    dep = InvestDeposit(id="dep_1", client_id="c_1", account_number="42301", balance=Decimal("10000.00"), interest_rate=Decimal("15.00"), term_months=12)
    dep_resp = DepositResponse.model_validate(dep)
    assert dep_resp.id == "dep_1"
    assert dep_resp.interest_rate == Decimal("15.00")
    assert dep_resp.term_months == 12

    sav = InvestSavings(id="sav_1", client_id="c_1", account_number="40812", balance=Decimal("5000.00"), interest_rate=Decimal("10.00"))
    sav_resp = SavingsAccountResponse.model_validate(sav)
    assert sav_resp.balance == Decimal("5000.00")

    broker = InvestBroker(id="brk_1", client_id="c_1", account_number="30601", balance=Decimal("20000.00"))
    broker_resp = BrokerAccountResponse.model_validate(broker)
    assert broker_resp.balance == Decimal("20000.00")

    portfolio = PortfolioResponse(
        savings_accounts=[sav_resp],
        deposits=[dep_resp],
        broker_accounts=[broker_resp],
    )
    assert len(portfolio.savings_accounts) == 1
    assert len(portfolio.deposits) == 1
    assert len(portfolio.broker_accounts) == 1
    assert "savings_accounts" in portfolio


def test_digital_ruble_response_models_validation():
    wallet = RubleWallet(id="w_1", client_id="c_1", wallet_number="DRUBLE_123", balance=Decimal("1000.00"))
    w_resp = WalletResponse.model_validate(wallet)
    assert w_resp.wallet_number == "DRUBLE_123"
    assert w_resp.balance == Decimal("1000.00")

    rtx = RubleTransaction(id="rtx_1", receiver_wallet_id="w_2", amount=Decimal("250.00"))
    rtx_resp = RubleTransactionResponse.model_validate(rtx)
    assert rtx_resp.amount == Decimal("250.00")

    contract = RubleContract(id="sc_1", creator_wallet_id="w_1", receiver_wallet_id="w_2", amount=Decimal("500.00"), condition_type="date")
    c_resp = SmartContractResponse.model_validate(contract)
    assert c_resp.condition_type == "date"
    assert c_resp.amount == Decimal("500.00")


def test_routers_define_response_models():
    """
    Проверяет, что ключевые маршруты в FastAPI роутерах имеют определенный response_model.
    """
    routes = {}
    for r in (bank_router, invest_router, ruble_router):
        for route in r.routes:
            for method in route.methods:
                routes[(route.path, method)] = route

    from typing import List

    # Маршруты банковского сервиса
    assert routes[("/accounts/by-client/{client_id}", "GET")].response_model in (List[AccountResponse], list[AccountResponse])
    assert routes[("/accounts/{account_id}/tariff", "GET")].response_model == TariffResponse
    assert routes[("/accounts/{account_id}/cards", "GET")].response_model in (List[CardResponse], list[CardResponse])
    assert routes[("/accounts/{account_id}/transactions", "GET")].response_model in (List[TransactionResponse], list[TransactionResponse])
    assert routes[("/accounts/{account_id}/autopayments", "GET")].response_model in (List[AutoPaymentResponse], list[AutoPaymentResponse])
    assert routes[("/accounts/{account_id}/autopayments", "POST")].response_model == AutoPaymentResponse
    assert routes[("/accounts/{account_id}/cards/issue", "POST")].response_model == CardResponse

    # Маршруты инвестиционного сервиса
    assert routes[("/strategies", "GET")].response_model in (List[InvestmentStrategyResponse], list[InvestmentStrategyResponse])
    assert routes[("/portfolio/{client_id}", "GET")].response_model == PortfolioResponse
    assert routes[("/deposits/open", "POST")].response_model == DepositResponse
    assert routes[("/savings/open", "POST")].response_model == SavingsAccountResponse
    assert routes[("/broker-accounts/open", "POST")].response_model == BrokerAccountResponse

    # Маршруты сервиса цифрового рубля
    assert routes[("/wallets/{client_id}", "GET")].response_model == WalletResponse
    assert routes[("/wallets/{wallet_id}/transactions", "GET")].response_model in (List[RubleTransactionResponse], list[RubleTransactionResponse])
    assert routes[("/wallets/{wallet_id}/transfers", "POST")].response_model == RubleTransactionResponse
    assert routes[("/wallets/{wallet_id}/smart-contracts", "GET")].response_model in (List[SmartContractResponse], list[SmartContractResponse])
    assert routes[("/wallets/{wallet_id}/smart-contracts", "POST")].response_model == SmartContractResponse


def test_admin_server_response_models():
    """
    Проверяет, что ключевые маршруты в admin_server имеют определенные response_model.
    """
    import sys
    from unittest.mock import MagicMock
    for mod in ["redis", "minio", "litellm", "asyncpg"]:
        if mod not in sys.modules:
            sys.modules[mod] = MagicMock()
    
    import os
    admin_dir = os.path.abspath("src/admin_server")
    fastapi_dir = os.path.abspath("src/admin_server/fastapi")
    if admin_dir not in sys.path:
        sys.path.insert(0, admin_dir)
    if fastapi_dir not in sys.path:
        sys.path.insert(0, fastapi_dir)

    from src.admin_server.fastapi.server import app
    from src.admin_server.fastapi.db import schemas
    
    routes = {}
    for route in app.routes:
        if hasattr(route, "methods"):
            for method in route.methods:
                routes[(route.path, method)] = route

    assert routes[("/login", "POST")].response_model.__name__ == "TokenResponse"
    assert routes[("/api/config/contentai", "GET")].response_model.__name__ == "ContentAiConfigResponse"
    assert routes[("/api/v1/rag/documents", "POST")].response_model.__name__ == "RAGDocumentResponse"
    assert routes[("/api/agent-requests/approve", "POST")].response_model.__name__ == "BatchActionResponse"
    assert routes[("/api/agent-requests/reject", "POST")].response_model.__name__ == "BatchActionResponse"


def test_to_dict_marked_deprecated():
    """
    Проверяет, что старый to_dict помечен как [DEPRECATED] в документации.
    """
    assert "[DEPRECATED]" in to_dict.__doc__
    assert "[DEPRECATED]" in to_dict_list.__doc__


def test_transaction_date_and_time_moscow_synchronization():
    """
    Проверяет, что дата и время транзакции синхронизированы по Московскому часовому поясу (UTC+3)
    и не рассинхронизируются на стыке суток (21:00-00:00 UTC).
    """
    from src.simulations.db.bank.db.models import get_moscow_date, get_moscow_time

    # 1. Проверяем функции по умолчанию
    m_date = get_moscow_date()
    m_time = get_moscow_time()
    now_utc = datetime.now(timezone.utc)
    expected_dt = now_utc + timedelta(hours=3)

    assert m_date == expected_dt.date()
    assert abs(m_time.hour - expected_dt.time().hour) <= 1

    # 2. Проверяем, что в модели Transaction используются именно московские колбэки
    assert BankTransaction.date.default.arg.__name__ == "get_moscow_date"
    assert BankTransaction.time.default.arg.__name__ == "get_moscow_time"
    assert BankTransaction.date.default.arg(None) == expected_dt.date()


@pytest.mark.anyio
async def test_generate_unique_card_number_collision_retry():
    """
    Проверяет работу механизма повторных попыток при коллизии номера карты.
    """
    from unittest.mock import AsyncMock
    from src.simulations.api.routers.bank import generate_unique_card_number

    mock_db = AsyncMock()
    # 1-я попытка возвращает коллизию (существующий id), 2-я попытка возвращает None (номер свободен)
    mock_db.scalar.side_effect = ["existing-card-id", None]

    card_num = await generate_unique_card_number(mock_db, max_attempts=5)
    assert card_num.startswith("4276")
    assert len(card_num) == 16
    assert mock_db.scalar.call_count == 2


@pytest.mark.anyio
async def test_generate_unique_invest_account_number_collision_retry():
    """
    Проверяет работу механизма повторных попыток при коллизии номера инвестиционного счета.
    """
    from unittest.mock import AsyncMock
    from src.simulations.api.routers.invest import generate_unique_invest_account_number
    from src.simulations.db.invest.db.models import Deposit

    mock_db = AsyncMock()
    # 1-я попытка - коллизия, 2-я попытка - свободен
    mock_db.scalar.side_effect = ["existing-dep-id", None]

    acc_num = await generate_unique_invest_account_number(mock_db, Deposit, prefix="4230", max_attempts=5)
    assert acc_num.startswith("4230")
    assert len(acc_num) == 20
    assert mock_db.scalar.call_count == 2

