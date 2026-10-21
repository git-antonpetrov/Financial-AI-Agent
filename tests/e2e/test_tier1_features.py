"""
tests/e2e/test_tier1_features.py
Tier 1: Feature Coverage in Isolation (Happy Path).
Validates primary behavior for all platform components under nominal conditions.
"""

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from src.admin_server.fastapi.rbac import Permission, Role, get_permissions_for_role
from src.admin_server.fastapi.security import (
    create_access_token,
    decode_access_token,
    generate_totp_secret,
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
    is_encrypted_chunk,
)
from src.simulations.api.routers.bank import TransferRequest, transfer_money
from src.simulations.core.crypto.chain_validator import X509ChainValidator
from src.simulations.db.bank.db.models import Account
from src.simulations.smart_contracts.dsl.context import ContractContext
from src.simulations.smart_contracts.sandbox.executor import SmartContractSandbox


def make_async_cm(session):
    if not hasattr(session, "add") or isinstance(session.add, AsyncMock):
        session.add = MagicMock()
    if hasattr(session, "scalar") and isinstance(session.scalar, AsyncMock):
        if isinstance(session.scalar.return_value, AsyncMock):
            session.scalar.return_value = None
    cm = MagicMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = None
    return cm


# -----------------------------------------------------------------------------
# 1. PKI & Cryptographic Identity
# -----------------------------------------------------------------------------
def test_pki_root_ca_and_leaf_validation(pki_ca_authority, cert_factory, tmp_path):
    """
    Tier 1 Feature: PKI & Zero-Trust Identity.
    Verifies Root CA parameters and end-entity X.509 chain validation.
    """
    ca_cert = pki_ca_authority["cert"]
    bc = ca_cert.extensions.get_extension_for_oid(x509.ExtensionOID.BASIC_CONSTRAINTS).value
    assert bc.ca is True
    assert bc.path_length == 2

    leaf = cert_factory("admin-client.simulation", is_client=True)
    leaf_cert = leaf["cert"]
    leaf_bc = leaf_cert.extensions.get_extension_for_oid(x509.ExtensionOID.BASIC_CONSTRAINTS).value
    assert leaf_bc.ca is False

    leaf_eku = leaf_cert.extensions.get_extension_for_oid(x509.ExtensionOID.EXTENDED_KEY_USAGE).value
    assert ExtendedKeyUsageOID.CLIENT_AUTH in leaf_eku

    ca_file = tmp_path / "ca.crt"
    leaf_file = tmp_path / "leaf.crt"
    ca_file.write_text(pki_ca_authority["cert_pem"], encoding="utf-8")
    leaf_file.write_text(leaf["cert_pem"], encoding="utf-8")

    validator = X509ChainValidator(ca_cert_path=str(ca_file))
    valid, msg = validator.validate_certificate_chain(str(leaf_file))
    assert valid is True, f"Chain validation failed: {msg}"


# -----------------------------------------------------------------------------
# 2. Authentication & 2FA TOTP
# -----------------------------------------------------------------------------
def test_totp_generation_and_verification():
    """
    Tier 1 Feature: 2FA TOTP RFC 6238.
    Verifies that generated TOTP secrets produce valid time-based codes.
    """
    import pyotp

    secret = generate_totp_secret()
    assert len(secret) >= 16

    totp = pyotp.TOTP(secret)
    current_code = totp.now()

    is_valid = verify_totp_code(secret, current_code)
    assert is_valid is True


def test_asymmetric_jwt_issuance_and_decoding():
    """
    Tier 1 Feature: RS256 JWT Authentication.
    Verifies asymmetric token signing and payload extraction.
    """
    payload_data = {"sub": "admin", "custom_claim": "test_e2e"}
    token = create_access_token(data=payload_data, role=Role.SUPERADMIN.value)
    assert isinstance(token, str) and len(token) > 20

    decoded = decode_access_token(token)
    assert decoded["sub"] == "admin"
    assert decoded["role"] == Role.SUPERADMIN.value
    assert decoded["type"] == "access"
    assert decoded["custom_claim"] == "test_e2e"


# -----------------------------------------------------------------------------
# 3. Role-Based Access Control (RBAC)
# -----------------------------------------------------------------------------
def test_rbac_roles_and_permissions_matrix():
    """
    Tier 1 Feature: RBAC Permission Matrix.
    Verifies that roles receive strictly partitioned permissions.
    """
    superadmin_perms = get_permissions_for_role(Role.SUPERADMIN)
    operator_perms = get_permissions_for_role(Role.OPERATOR)
    auditor_perms = get_permissions_for_role(Role.AUDITOR)

    assert Permission.DOCUMENTS_DELETE in superadmin_perms
    assert Permission.KEYS_REVOKE in superadmin_perms
    assert Permission.AUDIT_READ in superadmin_perms

    assert Permission.DOCUMENTS_READ in operator_perms
    assert Permission.SECURITY_MANAGE not in operator_perms
    assert Permission.KEYS_REVOKE not in operator_perms

    assert Permission.AUDIT_READ in auditor_perms
    assert Permission.AUDIT_VERIFY in auditor_perms
    assert Permission.DOCUMENTS_WRITE not in auditor_perms
    assert Permission.KEYS_ROTATE not in auditor_perms


# -----------------------------------------------------------------------------
# 4. Tamper-Evident Audit Logging
# -----------------------------------------------------------------------------
def test_audit_hash_chaining_integrity():
    """
    Tier 1 Feature: Tamper-Evident Audit Hash-Chaining.
    Verifies deterministic SHA-256 computation and chain link verification.
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

    details1_str = json.dumps({"reason": "initial boot"})
    h1 = compute_audit_hash(ts_str, "admin", "127.0.0.1", "INIT", "core", "SUCCESS", details1_str, GENESIS_HASH)
    entry1 = MockAuditEntry(1, GENESIS_HASH, now, "admin", "INIT", "SUCCESS", details1_str, h1)

    details2_str = json.dumps({"target": "bank_service"})
    h2 = compute_audit_hash(ts_str, "operator", "127.0.0.1", "CONFIG_UPDATE", "core", "SUCCESS", details2_str, h1)
    entry2 = MockAuditEntry(2, h1, now, "operator", "CONFIG_UPDATE", "SUCCESS", details2_str, h2)

    class MockDbSession:
        async def execute(self, stmt):
            m = MagicMock()
            m.scalars.return_value.all.return_value = [entry1, entry2]
            return m

    res = asyncio.run(verify_audit_chain(MockDbSession()))
    assert res["is_valid"] is True
    assert res["total_records"] == 2
    assert res["broken_record_id"] is None


# -----------------------------------------------------------------------------
# 5. Ingress Invariant Validation
# -----------------------------------------------------------------------------
def test_caddyfile_ingress_invariants():
    """
    Tier 1 Feature: Caddy Ingress Security Invariants.
    Verifies that Caddyfile enforces TLS 1.2/1.3, mTLS client_auth, and header sanitation.
    """
    caddy_path = Path("src/caddy/Caddyfile")
    assert caddy_path.exists(), "Caddyfile does not exist"
    content = caddy_path.read_text(encoding="utf-8")

    assert "protocols tls1.2 tls1.3" in content
    assert "client_auth" in content
    assert "trusted_ca_cert_file /etc/caddy/certs/ca.crt" in content
    assert "header_up -X-Client-Cert-*" in content
    assert "header_up X-Client-Cert-Verify {tls_client_verified}" in content
    assert "Strict-Transport-Security" in content


# -----------------------------------------------------------------------------
# 6. Banking Simulation Engine
# -----------------------------------------------------------------------------
def test_bank_transfer_nominal_execution(monkeypatch):
    """
    Tier 1 Feature: Banking Simulation Transfer Router.
    Verifies successful transfer, balance updates, and signed bank receipt generation.
    """
    async def _test():
        acc_sender = Account(
            id="acc_1",
            account_number="40817810000000000001",
            client_id="client_1",
            balance=Decimal("10000.00"),
            currency="RUB",
            status="active",
        )
        acc_receiver = Account(
            id="acc_2",
            account_number="40817810000000000002",
            client_id="client_2",
            balance=Decimal("2000.00"),
            currency="RUB",
            status="active",
        )

        session = AsyncMock()
        session.scalar = AsyncMock(return_value=acc_receiver)
        res_mock = MagicMock()
        res_mock.scalars.return_value.all.return_value = [acc_sender, acc_receiver]
        session.execute = AsyncMock(return_value=res_mock)
        session.commit = AsyncMock()

        monkeypatch.setattr("src.simulations.api.routers.bank.get_db", lambda: make_async_cm(session))

        req = TransferRequest(
            from_account_id="acc_1",
            amount=Decimal("1500.00"),
            transfer_type="account",
            destination="40817810000000000002",
            description="E2E test transfer",
        )

        result = await transfer_money(req)

        assert result["status"] == "success"
        assert acc_sender.balance == Decimal("8500.00")
        assert acc_receiver.balance == Decimal("3500.00")
        assert "signed_receipt" in result
        assert result["signed_receipt"]["receipt"]["action"] == "bank_transfer"
        assert result["signed_receipt"]["signature"] is not None

    asyncio.run(_test())


# -----------------------------------------------------------------------------
# 7. Smart Contract Sandbox
# -----------------------------------------------------------------------------
def test_smart_contract_sandbox_happy_path():
    """
    Tier 1 Feature: Smart Contract Sandbox Isolation & Execution.
    Verifies that a valid AST contract executes within bounds and yields transfers.
    """
    code = """
def execute(ctx):
    if ctx.get_condition_status() == 'approved':
        ctx.transfer(ctx.creator_id, ctx.receiver_id, ctx.amount)
        return True
    return False
"""
    ctx = ContractContext(
        contract_id="contract_e2e_1",
        creator_id="wallet_alice",
        receiver_id="wallet_bob",
        amount=Decimal("500.00"),
        condition_status="approved",
    )

    success, is_completed, transfers, error = SmartContractSandbox.execute_contract(code, ctx)

    assert success is True
    assert is_completed is True
    assert error is None
    assert len(transfers) == 1
    assert transfers[0] == {"from": "wallet_alice", "to": "wallet_bob", "amount": 500.0}


# -----------------------------------------------------------------------------
# 8. ChromaDB Envelope Encryption
# -----------------------------------------------------------------------------
def test_chromadb_envelope_encryption_roundtrip():
    """
    Tier 1 Feature: Vector Store Envelope Encryption (AES-256-GCM).
    Verifies that document vector chunks are encrypted at rest and decrypted accurately.
    """
    raw_text = "Sensitive financial report: Revenue grew by 25% in Q3."
    key = b"0123456789abcdef0123456789abcdef"  # 32 bytes

    encrypted_text = encrypt_chroma_chunk(raw_text, key=key)
    assert encrypted_text != raw_text
    assert is_encrypted_chunk(encrypted_text)

    decrypted_text = decrypt_chroma_chunk(encrypted_text, key=key)
    assert decrypted_text == raw_text
