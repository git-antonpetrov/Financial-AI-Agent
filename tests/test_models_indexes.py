import pytest
from src.simulations.db.bank.db.models import Account, Card, Transaction, AutoPayment
from src.simulations.db.invest.db.models import SavingsAccount, Deposit, BrokerAccount
from src.simulations.db.digital_ruble.db.models import Wallet, RubleTransaction, SmartContract


def assert_column_indexed(model, column_name: str):
    col = model.__table__.c[column_name]
    assert col.index is True, f"Column {model.__tablename__}.{column_name} is expected to have index=True"


def test_bank_models_indexes():
    # Account
    assert_column_indexed(Account, "client_id")
    assert_column_indexed(Account, "tariff_id")
    assert_column_indexed(Account, "status")

    # Card
    assert_column_indexed(Card, "account_id")
    assert_column_indexed(Card, "status")

    # Transaction
    assert_column_indexed(Transaction, "account_id")
    assert_column_indexed(Transaction, "date")

    # AutoPayment
    assert_column_indexed(AutoPayment, "account_id")
    assert_column_indexed(AutoPayment, "next_payment_date")
    assert_column_indexed(AutoPayment, "is_active")


def test_invest_models_indexes():
    # SavingsAccount
    assert_column_indexed(SavingsAccount, "client_id")
    assert_column_indexed(SavingsAccount, "status")
    assert_column_indexed(SavingsAccount, "next_payment_date")

    # Deposit
    assert_column_indexed(Deposit, "client_id")
    assert_column_indexed(Deposit, "status")
    assert_column_indexed(Deposit, "next_payment_date")

    # BrokerAccount
    assert_column_indexed(BrokerAccount, "client_id")
    assert_column_indexed(BrokerAccount, "strategy_id")
    assert_column_indexed(BrokerAccount, "status")
    assert_column_indexed(BrokerAccount, "next_commission_date")


def test_digital_ruble_models_indexes():
    # Wallet
    assert_column_indexed(Wallet, "client_id")
    assert_column_indexed(Wallet, "status")

    # RubleTransaction
    assert_column_indexed(RubleTransaction, "sender_wallet_id")
    assert_column_indexed(RubleTransaction, "receiver_wallet_id")
    assert_column_indexed(RubleTransaction, "smart_contract_id")
    assert_column_indexed(RubleTransaction, "status")
    assert_column_indexed(RubleTransaction, "timestamp")

    # SmartContract
    assert_column_indexed(SmartContract, "creator_wallet_id")
    assert_column_indexed(SmartContract, "receiver_wallet_id")
    assert_column_indexed(SmartContract, "condition_status")
    assert_column_indexed(SmartContract, "status")
