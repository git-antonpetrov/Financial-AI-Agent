"""
Модуль тестирования наличия индексов у ключевых колонок ORM-моделей симуляции.
"""

import pytest
from src.simulations.db.bank.db.models import Account, Card, Transaction, AutoPayment
from src.simulations.db.invest.db.models import SavingsAccount, Deposit, BrokerAccount
from src.simulations.db.digital_ruble.db.models import Wallet, RubleTransaction, SmartContract


def assert_column_indexed(model, column_name: str):
    """Проверяет, что у указанной колонки модели активирован параметр index=True."""
    col = model.__table__.c[column_name]
    assert col.index is True, f"Колонка {model.__tablename__}.{column_name} должна иметь флаг index=True"


def test_bank_models_indexes():
    """Проверяет наличие индексов у моделей банковского домена."""
    # Модель Account
    assert_column_indexed(Account, "client_id")
    assert_column_indexed(Account, "tariff_id")
    assert_column_indexed(Account, "status")

    # Модель Card
    assert_column_indexed(Card, "account_id")
    assert_column_indexed(Card, "status")

    # Модель Transaction
    assert_column_indexed(Transaction, "account_id")
    assert_column_indexed(Transaction, "date")

    # Модель AutoPayment
    assert_column_indexed(AutoPayment, "account_id")
    assert_column_indexed(AutoPayment, "next_payment_date")
    assert_column_indexed(AutoPayment, "is_active")


def test_invest_models_indexes():
    """Проверяет наличие индексов у моделей инвестиционного домена."""
    # Модель SavingsAccount
    assert_column_indexed(SavingsAccount, "client_id")
    assert_column_indexed(SavingsAccount, "status")
    assert_column_indexed(SavingsAccount, "next_payment_date")

    # Модель Deposit
    assert_column_indexed(Deposit, "client_id")
    assert_column_indexed(Deposit, "status")
    assert_column_indexed(Deposit, "next_payment_date")

    # Модель BrokerAccount
    assert_column_indexed(BrokerAccount, "client_id")
    assert_column_indexed(BrokerAccount, "strategy_id")
    assert_column_indexed(BrokerAccount, "status")
    assert_column_indexed(BrokerAccount, "next_commission_date")


def test_digital_ruble_models_indexes():
    """Проверяет наличие индексов у моделей цифрового рубля."""
    # Модель Wallet
    assert_column_indexed(Wallet, "client_id")
    assert_column_indexed(Wallet, "status")

    # Модель RubleTransaction
    assert_column_indexed(RubleTransaction, "sender_wallet_id")
    assert_column_indexed(RubleTransaction, "receiver_wallet_id")
    assert_column_indexed(RubleTransaction, "smart_contract_id")
    assert_column_indexed(RubleTransaction, "status")
    assert_column_indexed(RubleTransaction, "timestamp")

    # Модель SmartContract
    assert_column_indexed(SmartContract, "creator_wallet_id")
    assert_column_indexed(SmartContract, "receiver_wallet_id")
    assert_column_indexed(SmartContract, "condition_status")
    assert_column_indexed(SmartContract, "status")
