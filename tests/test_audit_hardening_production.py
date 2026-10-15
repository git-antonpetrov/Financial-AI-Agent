import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

# Добавляем пути к модулям admin_server и fastapi
if os.path.abspath("src/admin_server") not in sys.path:
    sys.path.insert(0, os.path.abspath("src/admin_server"))
if os.path.abspath("src/admin_server/fastapi") not in sys.path:
    sys.path.insert(0, os.path.abspath("src/admin_server/fastapi"))

MOCK_MODULES = [
    "redis",
    "minio",
    "litellm",
    "asyncpg",
    "psycopg2",
    "psycopg2.pool",
    "chromadb",
    "chromadb.config",
    "langchain_text_splitters",
]
for mod in MOCK_MODULES:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from src.admin_server.fastapi.rbac import Role
from src.admin_server.fastapi.security import (
    set_trusted_root_ca,
    validate_x509_certificate_chain,
)
from src.admin_server.fastapi.server import VALID_ADMIN_USERS
from src.simulations.api.routers.bank import TransferRequest, transfer_money
from src.simulations.api.routers.invest import (
    CloseProductRequest,
    OpenProductRequest,
    close_deposit,
    close_savings,
    open_deposit,
)
from src.simulations.db.bank.db.models import Account
from src.simulations.db.digital_ruble.db.models import SmartContract, Wallet
from src.simulations.db.invest.db.models import Deposit, SavingsAccount
from src.simulations.smart_contracts.sandbox.executor import (
    SandboxExecutionError,
    SmartContractSandbox,
)
from src.simulations.workers.digital_eod.tasks import process_smart_contracts


def make_async_cm(session):
    """Вспомогательный генератор асинхронного контекстного менеджера для сессий БД."""
    if not hasattr(session, "add") or isinstance(session.add, AsyncMock):
        session.add = MagicMock()
    cm = MagicMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = None
    return cm


# -----------------------------------------------------------------------------
# 1. Защита песочницы смарт-контрактов: блокировка comprehensions
# -----------------------------------------------------------------------------


def test_smart_contract_sandbox_blocks_comprehensions():
    """Проверяет, что AST-анализатор песочницы блокирует все виды генераторных выражений."""
    # 1. List comprehension
    code_list = "def execute(ctx):\n    a = [x for x in [1, 2, 3]]\n"
    with pytest.raises(SandboxExecutionError) as exc:
        SmartContractSandbox.validate_code(code_list)
    assert "списковых включений" in str(exc.value)

    # 2. Set comprehension
    code_set = "def execute(ctx):\n    a = {x for x in [1, 2, 3]}\n"
    with pytest.raises(SandboxExecutionError) as exc:
        SmartContractSandbox.validate_code(code_set)
    assert "set comprehensions" in str(exc.value)

    # 3. Dict comprehension
    code_dict = "def execute(ctx):\n    a = {x: x for x in [1, 2, 3]}\n"
    with pytest.raises(SandboxExecutionError) as exc:
        SmartContractSandbox.validate_code(code_dict)
    assert "dict comprehensions" in str(exc.value)

    # 4. Generator expression
    code_gen = "def execute(ctx):\n    a = (x for x in [1, 2, 3])\n"
    with pytest.raises(SandboxExecutionError) as exc:
        SmartContractSandbox.validate_code(code_gen)
    assert "генераторных выражений" in str(exc.value)


# -----------------------------------------------------------------------------
# 2. Межбанковские операции: запрет межвалютных переводов без конвертации
# -----------------------------------------------------------------------------


def test_bank_transfer_blocks_cross_currency(monkeypatch):
    """Проверяет, что перевод между счетами с разными валютами отклоняется с ошибкой 400."""

    async def _test():
        sender = Account(
            id="acc_rub",
            account_number="40817810000000000001",
            client_id="c_1",
            currency="RUB",
            balance=Decimal("1000.00"),
            status="active",
        )
        receiver = Account(
            id="acc_usd",
            account_number="40817810000000000002",
            client_id="c_2",
            currency="USD",
            balance=Decimal("50.00"),
            status="active",
        )

        session = AsyncMock()
        session.scalar = AsyncMock(return_value=receiver)
        mock_res = MagicMock()
        mock_res.scalars.return_value.all.return_value = [sender, receiver]
        session.execute = AsyncMock(return_value=mock_res)
        session.commit = AsyncMock()

        monkeypatch.setattr("src.simulations.api.routers.bank.get_db", lambda: make_async_cm(session))

        req = TransferRequest(
            from_account_id="acc_rub",
            to_account_id="acc_usd",
            amount=Decimal("100.00"),
            transfer_type="account",
            destination="40817810000000000002",
        )
        with pytest.raises(HTTPException) as exc:
            await transfer_money(req)

        assert exc.value.status_code == 400
        assert "Межвалютные переводы без конвертации не поддерживаются" in str(exc.value.detail)

    asyncio.run(_test())


# -----------------------------------------------------------------------------
# 3. IDOR защита в роутере инвестиций
# -----------------------------------------------------------------------------


def test_invest_open_deposit_blocks_idor(monkeypatch):
    """Проверяет, что при несовпадении client_id счета и запроса открытие депозита отклоняется (403)."""

    async def _test():
        bank_acc = Account(
            id="acc_bank",
            client_id="victim_client",
            currency="RUB",
            balance=Decimal("50000.00"),
            status="active",
        )
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=bank_acc)
        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: make_async_cm(session))

        req = OpenProductRequest(
            client_id="attacker_client",
            from_bank_account_id="acc_bank",
            initial_amount=Decimal("1000.00"),
            term_months=6,
        )
        with pytest.raises(HTTPException) as exc:
            await open_deposit(req)

        assert exc.value.status_code == 403
        assert "Банковский счет списания не принадлежит указанному клиенту" in str(exc.value.detail)

    asyncio.run(_test())


def test_invest_close_deposit_and_savings_block_idor(monkeypatch):
    """Проверяет, что при закрытии депозита или копилки возврат на чужой банковский счет отклоняется (403)."""

    async def _test():
        dep = Deposit(
            id="dep_target",
            client_id="victim_client",
            balance=Decimal("10000.00"),
            status="active",
        )
        attacker_bank_acc = Account(
            id="acc_attacker",
            client_id="attacker_client",
            currency="RUB",
            balance=Decimal("0.00"),
            status="active",
        )

        invest_session = AsyncMock()
        invest_session.scalar = AsyncMock(return_value=dep)
        invest_session.commit = AsyncMock()

        bank_session = AsyncMock()
        bank_session.scalar = AsyncMock(return_value=attacker_bank_acc)

        monkeypatch.setattr("src.simulations.api.routers.invest.invest_db", lambda: make_async_cm(invest_session))
        monkeypatch.setattr("src.simulations.api.routers.invest.bank_db", lambda: make_async_cm(bank_session))

        req = CloseProductRequest(to_bank_account_id="acc_attacker")
        with pytest.raises(HTTPException) as exc:
            await close_deposit("dep_target", req)

        assert exc.value.status_code == 403
        assert "Банковский счет зачисления не принадлежит владельцу" in str(exc.value.detail)

        # Аналогично для копилки (SavingsAccount)
        sav = SavingsAccount(
            id="sav_target",
            client_id="victim_client",
            balance=Decimal("5000.00"),
            status="active",
        )
        invest_session.scalar = AsyncMock(return_value=sav)
        with pytest.raises(HTTPException) as exc_sav:
            await close_savings("sav_target", req)

        assert exc_sav.value.status_code == 403
        assert "Банковский счет зачисления не принадлежит владельцу" in str(exc_sav.value.detail)

    asyncio.run(_test())


# -----------------------------------------------------------------------------
# 4. Проверка расширений X.509 сертификатов (BasicConstraints & KeyUsage)
# -----------------------------------------------------------------------------


def test_x509_chain_validator_extension_constraints():
    """Проверяет отклонение промежуточных CA без CA=True и конечных сертификатов с CA=True."""
    # 1. Генерация тестового Root CA
    ca_key = rsa.generate_private_key(65537, 2048, default_backend())
    now = datetime.now(timezone.utc)
    ca_subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Test Root CA"),
    ])
    root_ca = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .sign(ca_key, hashes.SHA256(), default_backend())
    )
    set_trusted_root_ca(root_ca)

    # 2. Невалидный Enclave CA (ca=False)
    enc_key = rsa.generate_private_key(65537, 2048, default_backend())
    enc_subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Invalid Enclave CA"),
    ])
    invalid_enclave_ca = (
        x509.CertificateBuilder()
        .subject_name(enc_subject)
        .issuer_name(ca_subject)
        .public_key(enc_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256(), default_backend())
    )

    # Leaf сертификат
    leaf_key = rsa.generate_private_key(65537, 2048, default_backend())
    leaf_subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Leaf Agent"),
    ])
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_subject)
        .issuer_name(enc_subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=7))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(enc_key, hashes.SHA256(), default_backend())
    )

    leaf_pem = leaf_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    enc_pem = invalid_enclave_ca.public_bytes(serialization.Encoding.PEM).decode("utf-8")

    with pytest.raises(HTTPException) as exc:
        validate_x509_certificate_chain(leaf_pem, enclave_cert_input=enc_pem)
    assert exc.value.status_code == 401
    assert "CA=True" in str(exc.value.detail)

    # 3. Невалидный leaf сертификат с ca=True
    invalid_leaf = (
        x509.CertificateBuilder()
        .subject_name(leaf_subject)
        .issuer_name(ca_subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=7))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256(), default_backend())
    )
    inv_leaf_pem = invalid_leaf.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    with pytest.raises(HTTPException) as exc_leaf:
        validate_x509_certificate_chain(inv_leaf_pem)
    assert exc_leaf.value.status_code == 401
    assert "CA=True запрещен" in str(exc_leaf.value.detail)


# -----------------------------------------------------------------------------
# 5. Маппинг ролей пользователей админки
# -----------------------------------------------------------------------------


def test_admin_server_users_role_mapping():
    """Проверяет корректность маппинга ролей для пользователей admin, operator, auditor."""
    assert "admin" in VALID_ADMIN_USERS
    assert "operator" in VALID_ADMIN_USERS
    assert "auditor" in VALID_ADMIN_USERS

    # Проверка типов ролей в системе
    assert Role.SUPERADMIN.value == "superadmin"
    assert Role.OPERATOR.value == "operator"
    assert Role.AUDITOR.value == "auditor"


# -----------------------------------------------------------------------------
# 6. Гарантия возврата замороженного депозита при сбое смарт-контракта
# -----------------------------------------------------------------------------


def test_digital_eod_tasks_refunds_on_execution_failure():
    """Проверяет, что при аварийном исключении в процессе EOD смарт-контракт возвращает депозит создателю."""

    async def _test():
        creator = Wallet(
            id="creator_w",
            balance=Decimal("600.00"),
            frozen_balance=Decimal("400.00"),
            status="active",
        )
        contract = SmartContract(
            id="sc_fail",
            creator_wallet_id="creator_w",
            receiver_wallet_id="receiver_w",
            amount=Decimal("400.00"),
            status="active",
            condition_type="automatic",
            contract_code="raise RuntimeError('Explosion')",
        )

        session = AsyncMock()
        mock_contracts = MagicMock()
        mock_contracts.scalars.return_value.all.return_value = [contract]
        session.execute = AsyncMock(return_value=mock_contracts)

        # Моделируем критический сбой транзакции внутри session.begin_nested()
        session.begin_nested = MagicMock(side_effect=RuntimeError("Simulated database failure"))
        # Метод session.get возвращает объект кошелька создателя для возврата средств
        session.get = AsyncMock(return_value=creator)
        session.commit = AsyncMock()

        await process_smart_contracts(session)

        # Баланс создателя должен быть разморожен и восстановлен
        assert creator.balance == Decimal("1000.00")
        assert creator.frozen_balance == Decimal("0.00")
        assert contract.status == "failed"

    asyncio.run(_test())
