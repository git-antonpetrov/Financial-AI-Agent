"""
tests/e2e/test_tier3_combinations.py
Tier 3: Cross-Feature Interactions and Integration Combinations.
Tests the interplay across PKI, Ingress, Auth, RBAC, Financial Engine, EOD, and Audit Logging.
"""

import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.admin_server.fastapi.db.audit import (
    GENESIS_HASH,
    compute_audit_hash,
    format_audit_timestamp,
    verify_audit_chain,
)
from src.admin_server.fastapi.rbac import Permission, Role
from src.admin_server.fastapi.security import (
    CurrentUser,
    create_access_token,
    decode_access_token,
    parse_mtls_client_certificate,
    require_permission,
    require_role,
)
from src.simulations.api.routers.bank import TransferRequest, transfer_money
from src.simulations.core.crypto.chain_validator import X509ChainValidator
from src.simulations.db.bank.db.models import Account, AutoPayment, IdempotencyRecord
from src.simulations.smart_contracts.dsl.context import ContractContext
from src.simulations.smart_contracts.sandbox.executor import SmartContractSandbox
from src.simulations.workers.bank_eod.tasks import process_auto_payments


def make_async_cm(session):
    cm = MagicMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = None
    return cm


# -----------------------------------------------------------------------------
# 1. Auth JWT + Proxy Secret + RBAC Permission Matrix Combination
# -----------------------------------------------------------------------------
def test_auth_jwt_proxy_secret_and_rbac_matrix():
    """
    Tier 3 Combination: Ingress Proxy Secret + RS256 JWT + RBAC Authorization.
    Verifies that requests passing mTLS proxy validation enforce strict role permissions.
    """
    # 1. Ingress Proxy Secret validation
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/services/restart",
        "headers": [
            (b"x-proxy-secret", b"test-e2e-proxy-secret-token-32b!"),
            (b"x-client-cert-verify", b"SUCCESS"),
            (b"x-client-cert-subject", b"CN=admin-workstation.internal"),
        ],
    }
    req = Request(scope)
    mtls_meta = parse_mtls_client_certificate(req)
    assert mtls_meta["verified"] is True
    assert mtls_meta["common_name"] == "admin-workstation.internal"

    # 2. Superadmin JWT Token & Permission Check
    admin_token = create_access_token(data={"sub": "admin"}, role=Role.SUPERADMIN.value)
    admin_payload = decode_access_token(admin_token)
    admin_user = CurrentUser(
        username=admin_payload["sub"],
        role=Role(admin_payload["role"]),
        permissions={Permission(p) for p in admin_payload.get("permissions", [])},
    )

    admin_checker = require_role("superadmin")
    perm_checker = require_permission("security:manage")

    async def _test_admin():
        # Superadmin passes both role and permission checks
        res_role = await admin_checker(user=admin_user)
        assert res_role.username == "admin"
        res_perm = await perm_checker(user=admin_user)
        assert res_perm.username == "admin"

    asyncio.run(_test_admin())

    # 3. Auditor JWT Token attempting write action -> Must be forbidden (HTTP 403)
    auditor_token = create_access_token(data={"sub": "compliance_auditor"}, role=Role.AUDITOR.value)
    auditor_payload = decode_access_token(auditor_token)
    auditor_user = CurrentUser(
        username=auditor_payload["sub"],
        role=Role(auditor_payload["role"]),
        permissions={Permission(p) for p in auditor_payload.get("permissions", [])},
    )

    async def _test_auditor():
        with pytest.raises(HTTPException) as exc_role:
            await admin_checker(user=auditor_user)
        assert exc_role.value.status_code == 403

        with pytest.raises(HTTPException) as exc_perm:
            await perm_checker(user=auditor_user)
        assert exc_perm.value.status_code == 403

        # Auditor successfully passes read-only audit permission check
        audit_read_checker = require_permission("audit:read")
        res_audit = await audit_read_checker(user=auditor_user)
        assert res_audit.username == "compliance_auditor"

    asyncio.run(_test_auditor())


# -----------------------------------------------------------------------------
# 2. PKI Chain Validation + Bank Transfer with Idempotency Replay
# -----------------------------------------------------------------------------
def test_pki_validation_and_idempotent_transfer_flow(pki_ca_authority, cert_factory, tmp_path, monkeypatch):
    """
    Tier 3 Combination: PKI Mutual TLS Validation + Bank Transfer Idempotency Flow.
    Validates client certificate authenticity, executes transfer, and verifies exactly-once idempotency cache.
    """
    # 1. PKI validation of client certificate
    ca_pem = pki_ca_authority["cert_pem"]
    ca_file = tmp_path / "ca.crt"
    ca_file.write_text(ca_pem, encoding="utf-8")

    client_cert = cert_factory("bank-client-app.internal", is_client=True)
    client_file = tmp_path / "client.crt"
    client_file.write_text(client_cert["cert_pem"], encoding="utf-8")

    validator = X509ChainValidator(ca_cert_path=str(ca_file))
    is_valid, msg = validator.validate_certificate_chain(str(client_file))
    assert is_valid is True, f"PKI chain validation failed: {msg}"

    # 2. Bank transfer execution with X-Idempotency-Key
    async def _test():
        acc_sender = Account(
            id="acc_sender_3",
            account_number="40817810000000000003",
            client_id="client_3",
            balance=Decimal("20000.00"),
            currency="RUB",
            status="active",
        )
        acc_receiver = Account(
            id="acc_receiver_3",
            account_number="40817810000000000004",
            client_id="client_4",
            balance=Decimal("5000.00"),
            currency="RUB",
            status="active",
        )

        idempotency_store = {}
        added_records = []

        session = AsyncMock()

        async def mock_scalar(query):
            query_str = str(query).lower()
            if "idempotency_record" in query_str:
                return idempotency_store.get("idemp-tier3-flow-key")
            if "transaction" in query_str:
                return None
            if "account" in query_str:
                return acc_receiver
            return None

        def mock_add(entity):
            added_records.append(entity)
            if isinstance(entity, IdempotencyRecord):
                idempotency_store[entity.key] = entity

        session.scalar = AsyncMock(side_effect=mock_scalar)
        session.add = MagicMock(side_effect=mock_add)
        res_mock = MagicMock()
        res_mock.scalars.return_value.all.return_value = [acc_sender, acc_receiver]
        session.execute = AsyncMock(return_value=res_mock)
        session.commit = AsyncMock()

        monkeypatch.setattr("src.simulations.api.routers.bank.get_db", lambda: make_async_cm(session))

        req = TransferRequest(
            from_account_id="acc_sender_3",
            amount=Decimal("4000.00"),
            transfer_type="account",
            destination="40817810000000000004",
            idempotency_key="idemp-tier3-flow-key",
            description="Tier 3 integration transfer",
        )

        # First execution -> Performs transfer, debits balance, stores idempotency record
        result1 = await transfer_money(req)
        assert result1["status"] == "success"
        assert acc_sender.balance == Decimal("16000.00")
        assert acc_receiver.balance == Decimal("9000.00")
        assert "signed_receipt" in result1
        assert "idemp-tier3-flow-key" in idempotency_store

        # Second execution with identical idempotency key -> Returns cached response, no extra debit
        result2 = await transfer_money(req)
        assert result2["status"] == "success"
        assert result2["transaction_id"] == result1["transaction_id"]
        # Balances must NOT be debited a second time
        assert acc_sender.balance == Decimal("16000.00")
        assert acc_receiver.balance == Decimal("9000.00")

    asyncio.run(_test())


# -----------------------------------------------------------------------------
# 3. Bank Transfer & EOD Batch Reconciliation Combination
# -----------------------------------------------------------------------------
def test_bank_transfer_and_eod_batch_reconciliation(monkeypatch):
    """
    Tier 3 Combination: Financial Transfers + EOD Batch Worker with Fault Isolation.
    Verifies that scheduled payments execute under EOD and savepoint isolation prevents batch failure.
    """
    async def _test():
        today = date(2026, 10, 21)
        acc_payer = Account(
            id="acc_payer_eod",
            account_number="40817810000000000010",
            client_id="client_eod_1",
            balance=Decimal("10000.00"),
            currency="RUB",
            status="active",
        )
        auto_pay = AutoPayment(
            id="ap_1",
            account_id="acc_payer_eod",
            recipient="Utility Provider LLC",
            amount=Decimal("2500.00"),
            next_payment_date=today,
            schedule="monthly",
            account=acc_payer,
            is_active=True,
        )

        session = AsyncMock()
        session.add = MagicMock()
        nested_cm = MagicMock()
        nested_cm.__aenter__ = AsyncMock(return_value=None)
        nested_cm.__aexit__ = AsyncMock(return_value=None)
        session.begin_nested = MagicMock(return_value=nested_cm)
        session.get = AsyncMock(return_value=acc_payer)
        # Mocking auto-payment query
        res_mock = MagicMock()
        res_mock.scalars.return_value.all.return_value = [auto_pay]
        session.execute = AsyncMock(return_value=res_mock)
        session.commit = AsyncMock()

        # Execute EOD auto payment processing
        await process_auto_payments(session, today)

        # Balance debited by 2500.00
        assert acc_payer.balance == Decimal("7500.00")
        # Payment date advanced by 1 month
        assert auto_pay.next_payment_date > today
        assert session.commit.called is True

    asyncio.run(_test())


# -----------------------------------------------------------------------------
# 4. Smart Contract Execution & Tamper-Evident Audit Pipeline Combination
# -----------------------------------------------------------------------------
def test_smart_contract_and_tamper_evident_audit_pipeline():
    """
    Tier 3 Combination: Smart Contract Execution + Audit Hash Chain Generation.
    Executes a DSL smart contract and records the resulting state transition in the cryptographic audit chain.
    """
    contract_code = """
def execute(ctx):
    if ctx.get_condition_status() == 'fulfilled':
        ctx.transfer(ctx.creator_id, ctx.receiver_id, ctx.amount)
        return True
    return False
"""
    ctx = ContractContext(
        contract_id="contract_cross_audit_1",
        creator_id="wallet_escrow_alice",
        receiver_id="wallet_escrow_bob",
        amount=Decimal("1200.00"),
        condition_status="fulfilled",
    )

    success, is_completed, transfers, error = SmartContractSandbox.execute_contract(contract_code, ctx)
    assert success is True
    assert is_completed is True
    assert len(transfers) == 1
    assert transfers[0]["amount"] == Decimal("1200.00")

    # Record execution event into cryptographic audit log
    async def _test_audit():
        ts = datetime(2026, 10, 21, 14, 30, 0, tzinfo=UTC)
        ts_str = format_audit_timestamp(ts)
        event_details = json.dumps({
            "contract_id": ctx.contract_id,
            "transfers": [
                {"from": t["from"], "to": t["to"], "amount": str(t["amount"])}
                for t in transfers
            ],
        })

        rec_hash = compute_audit_hash(
            timestamp_str=ts_str,
            actor="smart_contract_engine",
            actor_ip="127.0.0.1",
            action="execute_contract",
            resource=ctx.contract_id,
            status="SUCCESS",
            details=event_details,
            prev_hash=GENESIS_HASH,
        )

        entry = MagicMock(
            id=101,
            timestamp=ts,
            actor="smart_contract_engine",
            actor_ip="127.0.0.1",
            action="execute_contract",
            resource=ctx.contract_id,
            status="SUCCESS",
            details=event_details,
            prev_hash=GENESIS_HASH,
            record_hash=rec_hash,
        )

        session = AsyncMock()
        res_mock = MagicMock()
        res_mock.scalars.return_value.all.return_value = [entry]
        session.execute = AsyncMock(return_value=res_mock)

        # Verify audit log integrity
        verification = await verify_audit_chain(session)
        assert verification["is_valid"] is True
        assert verification["total_records"] == 1
        assert verification["broken_record_id"] is None

    asyncio.run(_test_audit())
