"""
tests/e2e/test_tier2_boundaries.py
Tier 2: Boundary Conditions & Negative Rejections.
Validates fail-closed zero-trust invariants, unauthorized access rejections,
tampering detection, and edge-case boundary enforcement.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from pydantic import ValidationError

from src.admin_server.fastapi.rbac import Role
from src.admin_server.fastapi.security import (
    HTTPException,
    create_access_token,
    decode_access_token,
    generate_totp_secret,
    verify_mtls_client_identity,
    verify_totp_code,
)
from src.admin_server.fastapi.db.audit import (
    GENESIS_HASH,
    compute_audit_hash,
    format_audit_timestamp,
    verify_audit_chain,
)
from src.admin_server.vectors.chroma_envelope import (
    decrypt_chroma_chunk,
    encrypt_chroma_chunk,
)
from src.simulations.api.routers.bank import TransferRequest, transfer_money
from src.simulations.core.crypto.chain_validator import X509ChainValidator
from src.simulations.db.bank.db.models import Account
from src.simulations.smart_contracts.dsl.context import ContractContext
from src.simulations.smart_contracts.sandbox.executor import SmartContractSandbox
from tests.e2e.test_tier1_features import make_async_cm


# -----------------------------------------------------------------------------
# 1. PKI Boundary & Negative Validation
# -----------------------------------------------------------------------------
def test_pki_expired_certificate_rejection(pki_ca_authority, cert_factory, tmp_path):
    """
    Tier 2 Boundary: PKI Lifetime Enforcement.
    Verifies that certificates past their validity lifetime are strictly rejected.
    """
    ca_file = tmp_path / "ca.crt"
    ca_file.write_text(pki_ca_authority["cert_pem"], encoding="utf-8")
    validator = X509ChainValidator(ca_cert_path=str(ca_file))

    leaf = cert_factory("client-expired", is_client=True)
    future_time = datetime.now(timezone.utc) + timedelta(days=5000)
    valid, msg = validator.validate_certificate_lifetime(leaf["cert"], at_time=future_time)
    assert valid is False
    assert len(msg) > 0


def test_pki_untrusted_self_signed_rejection(pki_ca_authority, cert_factory, tmp_path):
    """
    Tier 2 Boundary: PKI Untrusted Issuer Rejection.
    Verifies that self-signed certificates with untrusted roots fail chain validation.
    """
    ca_file = tmp_path / "ca.crt"
    ca_file.write_text(pki_ca_authority["cert_pem"], encoding="utf-8")
    validator = X509ChainValidator(ca_cert_path=str(ca_file))

    untrusted = cert_factory("attacker-self-signed", is_client=True, self_signed=True)
    untrusted_file = tmp_path / "untrusted.crt"
    untrusted_file.write_text(untrusted["cert_pem"], encoding="utf-8")

    valid, msg = validator.validate_certificate_chain(str(untrusted_file))
    assert valid is False
    assert len(msg) > 0


def test_pki_leaf_as_ca_rejection(pki_ca_authority, cert_factory, tmp_path):
    """
    Tier 2 Boundary: Basic Constraints Violation Rejection.
    Verifies that an end-entity leaf attempting to claim BasicConstraints CA=True is rejected.
    """
    ca_file = tmp_path / "ca.crt"
    ca_file.write_text(pki_ca_authority["cert_pem"], encoding="utf-8")
    validator = X509ChainValidator(ca_cert_path=str(ca_file))

    fake_ca = cert_factory("malicious-leaf-as-ca", is_client=True, is_ca=True)
    fake_ca_file = tmp_path / "fake_ca.crt"
    fake_ca_file.write_text(fake_ca["cert_pem"], encoding="utf-8")

    valid, msg = validator.validate_certificate_chain(str(fake_ca_file))
    assert valid is False
    assert len(msg) > 0


# -----------------------------------------------------------------------------
# 2. Authentication & Ingress Boundary Rejections
# -----------------------------------------------------------------------------
def test_jwt_expired_token_rejection():
    """
    Tier 2 Boundary: Expired JWT Token Rejection.
    Verifies that tokens beyond expiration delta raise HTTPException 401 or PyJWT error.
    """
    from src.admin_server.fastapi.security import settings

    past_time = datetime.now(timezone.utc) - timedelta(seconds=60)
    expired_payload = {
        "sub": "admin",
        "role": Role.SUPERADMIN.value,
        "type": "access",
        "iat": past_time - timedelta(minutes=5),
        "exp": past_time,
        "jti": "expired-id",
    }
    expired_token = jwt.encode(expired_payload, settings.PRIVATE_KEY, algorithm=settings.ALGORITHM)
    with pytest.raises((HTTPException, jwt.PyJWTError)):
        decode_access_token(expired_token)


def test_jwt_tampered_signature_rejection():
    """
    Tier 2 Boundary: Cryptographic Signature Tampering Rejection.
    Verifies that any modification to JWT signature bytes raises HTTPException 401 or PyJWT error.
    """
    token = create_access_token(data={"sub": "admin"}, role=Role.SUPERADMIN.value)
    parts = token.split(".")
    # Mutate a character in the signature preserving base64 formatting
    sig = list(parts[2])
    sig[0] = "A" if sig[0] != "A" else "B"
    tampered_token = f"{parts[0]}.{parts[1]}.{''.join(sig)}"
    with pytest.raises((HTTPException, jwt.PyJWTError)) as exc_info:
        decode_access_token(tampered_token)
    if isinstance(exc_info.value, HTTPException):
        assert exc_info.value.status_code == 401


def test_totp_invalid_code_rejection():
    """
    Tier 2 Boundary: 2FA TOTP False Code Rejection.
    Verifies that invalid or out-of-sync codes are rejected.
    """
    secret = generate_totp_secret()
    assert verify_totp_code(secret, "000000") is False
    assert verify_totp_code(secret, "999999") is False
    assert verify_totp_code(secret, "invalid-totp") is False


def test_proxy_secret_rejection_on_mismatch_or_tampering():
    """
    Tier 2 Boundary: Reverse Proxy Untrusted Header Rejection.
    Verifies that requests bearing an invalid X-Proxy-Secret fail closed.
    """
    req = MagicMock()
    req.headers = {"x-proxy-secret": "forged-untrusted-proxy-token"}
    status = verify_mtls_client_identity(req)
    assert status["verified"] is False
    assert status["raw_verify_status"] == "UNTRUSTED_PROXY"


# -----------------------------------------------------------------------------
# 3. Audit Hash Chain Tamper Detection
# -----------------------------------------------------------------------------
def test_audit_hash_chain_tamper_detection():
    """
    Tier 2 Boundary: Tamper-Evident Chain Invalidation.
    Verifies that altering record hash or prev_hash breaks the cryptographic chain.
    """
    class MockAuditEntry:
        def __init__(self, record_id, prev_hash, timestamp, actor, action, status, details, record_hash, actor_ip="127.0.0.1", resource="core"):
            self.id = record_id
            self.prev_hash = prev_hash
            self.timestamp = timestamp
            self.actor = actor
            self.action = action
            self.status = status
            self.details = details
            self.record_hash = record_hash
            self.actor_ip = actor_ip
            self.resource = resource

    now = datetime.now(timezone.utc)
    ts_str = format_audit_timestamp(now)

    details1_str = json.dumps({"event": "genesis"})
    h1 = compute_audit_hash(ts_str, "admin", "127.0.0.1", "INIT", "core", "SUCCESS", details1_str, GENESIS_HASH)
    entry1 = MockAuditEntry(1, GENESIS_HASH, now, "admin", "INIT", "SUCCESS", details1_str, h1)

    details2_str = json.dumps({"event": "grant_role"})
    _ = compute_audit_hash(ts_str, "admin", "127.0.0.1", "ROLE_ASSIGN", "core", "SUCCESS", details2_str, h1)
    entry2_tampered = MockAuditEntry(2, h1, now, "admin", "ROLE_ASSIGN", "SUCCESS", details2_str, "corrupted_hash_value")

    class MockDbSession:
        async def execute(self, stmt):
            m = MagicMock()
            m.scalars.return_value.all.return_value = [entry1, entry2_tampered]
            return m

    res = asyncio.run(verify_audit_chain(MockDbSession()))
    assert res["is_valid"] is False
    assert res["broken_record_id"] == 2


# -----------------------------------------------------------------------------
# 4. Banking Simulation Invariants & Boundaries
# -----------------------------------------------------------------------------
def test_banking_transfer_overdraft_rejection(monkeypatch):
    """
    Tier 2 Boundary: Account Overdraft Rejection.
    Verifies that transfer amounts exceeding account balance raise HTTPException 400.
    """
    async def _test():
        acc_sender = Account(
            id="acc_1",
            account_number="40817810000000000001",
            client_id="client_1",
            balance=Decimal("100.00"),
            currency="RUB",
            status="active",
        )
        acc_receiver = Account(
            id="acc_2",
            account_number="40817810000000000002",
            client_id="client_2",
            balance=Decimal("500.00"),
            currency="RUB",
            status="active",
        )

        session = AsyncMock()
        session.scalar = AsyncMock(return_value=acc_receiver)
        res_mock = MagicMock()
        res_mock.scalars.return_value.all.return_value = [acc_sender, acc_receiver]
        session.execute = AsyncMock(return_value=res_mock)

        monkeypatch.setattr("src.simulations.api.routers.bank.get_db", lambda: make_async_cm(session))

        req = TransferRequest(
            from_account_id="acc_1",
            amount=Decimal("50000.00"),
            transfer_type="account",
            destination="40817810000000000002",
        )

        with pytest.raises(HTTPException) as exc_info:
            await transfer_money(req)
        assert exc_info.value.status_code == 400
        assert "Недостаточно средств" in exc_info.value.detail

    asyncio.run(_test())


def test_banking_transfer_negative_or_zero_amount():
    """
    Tier 2 Boundary: Non-Positive Amount Validation.
    Verifies that zero or negative transfer amounts are rejected by Pydantic / router.
    """
    with pytest.raises((HTTPException, ValidationError)):
        TransferRequest(
            from_account_id="acc_1",
            amount=Decimal("0.00"),
            transfer_type="account",
            destination="40817810000000000002",
        )

    with pytest.raises((HTTPException, ValidationError)):
        TransferRequest(
            from_account_id="acc_1",
            amount=Decimal("-150.00"),
            transfer_type="account",
            destination="40817810000000000002",
        )


def test_banking_transfer_same_account_rejection(monkeypatch):
    """
    Tier 2 Boundary: Self-Transfer Rejection.
    Verifies that transferring money to the same source account raises HTTPException 400.
    """
    async def _test():
        acc_sender = Account(
            id="acc_1",
            account_number="40817810000000000001",
            client_id="client_1",
            balance=Decimal("1000.00"),
            currency="RUB",
            status="active",
        )

        session = AsyncMock()
        session.scalar = AsyncMock(return_value=acc_sender)
        res_mock = MagicMock()
        res_mock.scalars.return_value.all.return_value = [acc_sender]
        session.execute = AsyncMock(return_value=res_mock)

        monkeypatch.setattr("src.simulations.api.routers.bank.get_db", lambda: make_async_cm(session))

        req = TransferRequest(
            from_account_id="acc_1",
            amount=Decimal("100.00"),
            transfer_type="account",
            destination="40817810000000000001",
        )

        with pytest.raises(HTTPException) as exc_info:
            await transfer_money(req)
        assert exc_info.value.status_code == 400

    asyncio.run(_test())


# -----------------------------------------------------------------------------
# 5. Smart Contract Sandbox AST Security
# -----------------------------------------------------------------------------
def test_smart_contract_sandbox_forbidden_ast():
    """
    Tier 2 Boundary: Sandbox AST Whitelist Enforcement.
    Verifies that forbidden syntax (modules, infinite loops, introspection) is rejected.
    """
    ctx = ContractContext(
        contract_id="contract_boundary_1",
        creator_id="alice",
        receiver_id="bob",
        amount=Decimal("100.00"),
        condition_status="approved",
    )

    # 1. Forbidden import
    code_import = "import os\ndef execute(ctx): return True"
    success, is_comp, transfers, err = SmartContractSandbox.execute_contract(code_import, ctx)
    assert success is False
    assert err is not None

    # 2. Forbidden while loop
    code_while = "def execute(ctx):\n    while True:\n        break\n    return True"
    success, is_comp, transfers, err = SmartContractSandbox.execute_contract(code_while, ctx)
    assert success is False
    assert err is not None

    # 3. Forbidden __class__ / reflection access
    code_dunder = "def execute(ctx):\n    x = ctx.__class__\n    return True"
    success, is_comp, transfers, err = SmartContractSandbox.execute_contract(code_dunder, ctx)
    assert success is False
    assert err is not None


# -----------------------------------------------------------------------------
# 6. ChromaDB Vector Envelope Encryption Boundaries
# -----------------------------------------------------------------------------
def test_chroma_envelope_corrupted_ciphertext_rejection():
    """
    Tier 2 Boundary: Envelope Encryption Authentication Tag Verification.
    Verifies that tampered or corrupted ciphertext fails authentication and raises ValueError.
    """
    raw_text = "Highly classified risk report"
    key = b"0123456789abcdef0123456789abcdef"

    encrypted_text = encrypt_chroma_chunk(raw_text, key=key)
    corrupted_text = encrypted_text[:-4] + "dead"

    with pytest.raises(ValueError):
        decrypt_chroma_chunk(corrupted_text, key=key)
