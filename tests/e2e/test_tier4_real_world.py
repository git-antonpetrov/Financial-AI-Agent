"""
tests/e2e/test_tier4_real_world.py
Tier 4: Real-World Workload and Complete Financial Lifecycle Scenarios.
Simulates end-to-end multi-step financial flows across identity, ingress, banking,
smart contracts, EOD batch reconciliation, and cryptographic audit chain verification.
"""

import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from starlette.requests import Request

from src.admin_server.fastapi.db.audit import (
    GENESIS_HASH,
    compute_audit_hash,
    format_audit_timestamp,
    verify_audit_chain,
)
from src.admin_server.fastapi.rbac import Role
from src.admin_server.fastapi.security import (
    create_access_token,
    decode_access_token,
    parse_mtls_client_certificate,
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


def test_full_financial_lifecycle_workflow(pki_ca_authority, cert_factory, tmp_path, monkeypatch):
    """
    Tier 4 Real-World Scenario: Complete 5-Stage Financial Lifecycle.
    Stage 1: Client mTLS authentication & RS256 JWT issuance with Ingress validation.
    Stage 2: Banking transfer with row-locking and idempotency key deduplication.
    Stage 3: Digital Ruble smart contract execution under AST sandbox and escrow settlement.
    Stage 4: Bank End-of-Day (EOD) batch reconciliation with savepoint fault isolation.
    Stage 5: Cryptographic audit chain mathematical integrity verification across all stages.
    """
    # =========================================================================
    # STAGE 1: Client Identity, Zero-Trust Ingress & Token Issuance
    # =========================================================================
    ca_pem = pki_ca_authority["cert_pem"]
    ca_file = tmp_path / "ca.crt"
    ca_file.write_text(ca_pem, encoding="utf-8")

    client_cert = cert_factory("operator-terminal.internal", is_client=True)
    client_file = tmp_path / "client.crt"
    client_file.write_text(client_cert["cert_pem"], encoding="utf-8")

    validator = X509ChainValidator(ca_cert_path=str(ca_file))
    cert_valid, cert_msg = validator.validate_certificate_chain(str(client_file))
    assert cert_valid is True, f"Stage 1: Certificate validation failed: {cert_msg}"

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/bank/transfer",
        "headers": [
            (b"x-proxy-secret", b"test-e2e-proxy-secret-token-32b!"),
            (b"x-client-cert-verify", b"SUCCESS"),
            (b"x-client-cert-subject", b"CN=operator-terminal.internal"),
        ],
    }
    proxy_req = Request(scope)
    ingress_status = parse_mtls_client_certificate(proxy_req)
    assert ingress_status["verified"] is True
    assert ingress_status["common_name"] == "operator-terminal.internal"

    token = create_access_token(data={"sub": "operator_alice"}, role=Role.OPERATOR.value)
    token_claims = decode_access_token(token)
    assert token_claims["sub"] == "operator_alice"
    assert token_claims["role"] == Role.OPERATOR.value

    # =========================================================================
    # STAGE 2: Banking Transfer with Deterministic Locks & Idempotency
    # =========================================================================
    async def _stage2_stage3_stage4_stage5():
        audit_trail = []

        # Audit helper to append tamper-evident records
        def append_audit_entry(actor, action, resource, details_dict):
            prev_hash = audit_trail[-1].record_hash if audit_trail else GENESIS_HASH
            ts = datetime.now(UTC)
            ts_str = format_audit_timestamp(ts)
            details_str = json.dumps(details_dict)
            rec_hash = compute_audit_hash(
                timestamp_str=ts_str,
                actor=actor,
                actor_ip="10.0.1.5",
                action=action,
                resource=resource,
                status="SUCCESS",
                details=details_str,
                prev_hash=prev_hash,
            )
            entry = MagicMock(
                id=len(audit_trail) + 1,
                timestamp=ts,
                actor=actor,
                actor_ip="10.0.1.5",
                action=action,
                resource=resource,
                status="SUCCESS",
                details=details_str,
                prev_hash=prev_hash,
                record_hash=rec_hash,
            )
            audit_trail.append(entry)

        # Record Stage 1 in audit log
        append_audit_entry(
            actor="operator_alice",
            action="authenticate",
            resource="ingress_gateway",
            details_dict={"client_cn": "operator-terminal.internal", "role": "operator"},
        )

        # Stage 2: Transfer execution
        acc_sender = Account(
            id="acc_stage2_sender",
            account_number="40817810000000000021",
            client_id="client_alice",
            balance=Decimal("50000.00"),
            currency="RUB",
            status="active",
        )
        acc_receiver = Account(
            id="acc_stage2_receiver",
            account_number="40817810000000000022",
            client_id="client_bob",
            balance=Decimal("10000.00"),
            currency="RUB",
            status="active",
        )

        idempotency_cache = {}
        session = AsyncMock()
        session.add = MagicMock()

        async def mock_bank_scalar(query):
            q_str = str(query).lower()
            if "idempotency_record" in q_str:
                return idempotency_cache.get("idemp-lifecycle-key-1")
            if "transaction" in q_str:
                return None
            if "account" in q_str:
                return acc_receiver
            return None

        def mock_bank_add(entity):
            if isinstance(entity, IdempotencyRecord):
                idempotency_cache[entity.key] = entity

        session.scalar = AsyncMock(side_effect=mock_bank_scalar)
        session.add = MagicMock(side_effect=mock_bank_add)
        res_mock = MagicMock()
        res_mock.scalars.return_value.all.return_value = [acc_sender, acc_receiver]
        session.execute = AsyncMock(return_value=res_mock)
        session.commit = AsyncMock()

        monkeypatch.setattr("src.simulations.api.routers.bank.get_db", lambda: make_async_cm(session))

        transfer_cmd = TransferRequest(
            from_account_id="acc_stage2_sender",
            amount=Decimal("15000.00"),
            transfer_type="account",
            destination="40817810000000000022",
            idempotency_key="idemp-lifecycle-key-1",
            description="Initial lifecycle collateral funding",
        )

        tx_res = await transfer_money(transfer_cmd)
        assert tx_res["status"] == "success"
        assert acc_sender.balance == Decimal("35000.00")
        assert acc_receiver.balance == Decimal("25000.00")
        assert "signed_receipt" in tx_res

        # Idempotency replay check
        replay_res = await transfer_money(transfer_cmd)
        assert replay_res["status"] == "success"
        assert replay_res["transaction_id"] == tx_res["transaction_id"]
        # Balances untouched
        assert acc_sender.balance == Decimal("35000.00")
        assert acc_receiver.balance == Decimal("25000.00")

        append_audit_entry(
            actor="operator_alice",
            action="bank_transfer",
            resource=tx_res["transaction_id"],
            details_dict={"amount": "15000.00", "sender": "acc_stage2_sender", "receiver": "acc_stage2_receiver"},
        )

        # =========================================================================
        # STAGE 3: Smart Contract Escrow Execution & Condition Settlement
        # =========================================================================
        contract_code = """
def execute(ctx):
    if ctx.get_condition_status() == 'fulfilled':
        # Transfer 10,000.00 from locked escrow to receiver
        ctx.transfer(ctx.creator_id, ctx.receiver_id, 10000.0)
        return True
    return False
"""
        contract_ctx = ContractContext(
            contract_id="contract_lifecycle_escrow_1",
            creator_id="wallet_alice_digital",
            receiver_id="wallet_bob_digital",
            amount=Decimal("10000.00"),
            condition_status="fulfilled",
        )

        sc_success, sc_completed, sc_transfers, sc_err = SmartContractSandbox.execute_contract(
            contract_code, contract_ctx
        )
        assert sc_success is True, f"Stage 3 error: {sc_err}"
        assert sc_completed is True
        assert len(sc_transfers) == 1
        assert sc_transfers[0]["amount"] == Decimal("10000.00")

        append_audit_entry(
            actor="smart_contract_engine",
            action="settle_contract",
            resource=contract_ctx.contract_id,
            details_dict={"transfers": [{"amount": "10000.00", "to": "wallet_bob_digital"}]},
        )

        # =========================================================================
        # STAGE 4: End-of-Day (EOD) Batch Reconciliation
        # =========================================================================
        today = date(2026, 10, 21)
        acc_reconciliation = Account(
            id="acc_stage4_eod",
            account_number="40817810000000000030",
            client_id="client_charlie",
            balance=Decimal("20000.00"),
            currency="RUB",
            status="active",
        )
        auto_pay = AutoPayment(
            id="ap_lifecycle_1",
            account_id="acc_stage4_eod",
            recipient="Cloud Hosting Co",
            amount=Decimal("5000.00"),
            next_payment_date=today,
            schedule="monthly",
            account=acc_reconciliation,
            is_active=True,
        )

        eod_session = AsyncMock()
        eod_session.add = MagicMock()
        nested_cm = MagicMock()
        nested_cm.__aenter__ = AsyncMock(return_value=None)
        nested_cm.__aexit__ = AsyncMock(return_value=None)
        eod_session.begin_nested = MagicMock(return_value=nested_cm)
        eod_session.get = AsyncMock(return_value=acc_reconciliation)
        eod_res_mock = MagicMock()
        eod_res_mock.scalars.return_value.all.return_value = [auto_pay]
        eod_session.execute = AsyncMock(return_value=eod_res_mock)
        eod_session.commit = AsyncMock()

        await process_auto_payments(eod_session, today)
        assert acc_reconciliation.balance == Decimal("15000.00")
        assert auto_pay.next_payment_date > today

        append_audit_entry(
            actor="bank_eod_worker",
            action="process_eod_autopayment",
            resource=auto_pay.id,
            details_dict={"debited_amount": "5000.00", "remaining_balance": "15000.00"},
        )

        # =========================================================================
        # STAGE 5: Cryptographic Audit Chain Verification
        # =========================================================================
        assert len(audit_trail) == 4

        audit_session = AsyncMock()
        audit_res_mock = MagicMock()
        audit_res_mock.scalars.return_value.all.return_value = audit_trail
        audit_session.execute = AsyncMock(return_value=audit_res_mock)

        verification_result = await verify_audit_chain(audit_session)
        assert verification_result["is_valid"] is True
        assert verification_result["total_records"] == 4
        assert verification_result["broken_record_id"] is None
        assert "подтверждена" in verification_result["details"]

    asyncio.run(_stage2_stage3_stage4_stage5())
