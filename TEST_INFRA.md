# Financial-AI-Agent Test Infrastructure Specification

## 1. Overview
This document specifies the test architecture, runner configuration, and execution guidelines for the **Financial-AI-Agent** platform. The test suite validates zero-trust invariants, cryptographic identity verification, financial idempotency, and fault isolation across all foundation microservices.

---

## 2. Test Architecture: 4-Tier Opaque-Box Suite

The end-to-end verification suite is located in `tests/e2e/` and organized into 4 distinct verification tiers:

### Tier 1: Feature Coverage in Isolation (`tests/e2e/test_tier1_features.py`)
- **Focus**: Validates primary nominal behavior (happy path) for every core component in isolation.
- **Coverage**:
  - Root CA parameters and leaf certificate issuance (`X509ChainValidator`).
  - RFC 6238 2FA TOTP secret generation and verification (`pyotp`).
  - Asymmetric RS256 JWT access token signing and claims verification.
  - Role-Based Access Control (RBAC) permission resolution per role.
  - Immutable SHA-256 hash-chain audit log formatting and Genesis block verification.
  - Caddy Ingress configuration and security header propagation.
  - Banking transfer router execution and signed bank receipt generation.
  - Smart contract DSL sandbox execution and token transfer emission.
  - ChromaDB vector chunk AES-256-GCM envelope encryption and decryption roundtrip.

### Tier 2: Boundary & Corner Cases (`tests/e2e/test_tier2_boundaries.py`)
- **Focus**: Negative validation, malformed inputs, limit conditions, and security boundaries.
- **Coverage**:
  - Expired X.509 certificate rejection.
  - Untrusted self-signed certificate rejection.
  - Expired RS256 JWT signature rejection (`jwt.ExpiredSignatureError`).
  - Cryptographically tampered JWT signature rejection (`jwt.PyJWTError`).
  - Invalid and out-of-sync TOTP code rejection.
  - Tampered or absent `X-Proxy-Secret` ingress rejection (`UNTRUSTED_PROXY`).
  - Tampered record detection in hash-chained audit logs (`verify_audit_chain`).
  - Bank transfer overdraft rejection with HTTP 400 (`Недостаточно средств`).
  - Zero and negative transfer amounts rejection via Pydantic schemas.
  - Self-transfer rejection to identical account with HTTP 400.
  - AST-level forbidden construct rejection in smart contracts (`import`, `eval`, `exec`, `open`, `__class__`).
  - Smart contract transfer amounts exceeding locked collateral deposit rejection.
  - ChromaDB corrupted ciphertext and wrong decryption key rejection (`ValueError`).

### Tier 3: Cross-Feature Interactions (`tests/e2e/test_tier3_combinations.py`)
- **Focus**: Interplay across multiple subsystems and protocols.
- **Coverage**:
  - Ingress `X-Proxy-Secret` + RS256 JWT + RBAC permission matrix (Superadmin vs Auditor write permissions).
  - PKI mutual TLS certificate validation + Bank transfer with exactly-once `X-Idempotency-Key` deduplication.
  - Banking transfer execution + End-of-Day (EOD) batch reconciliation with nested savepoints (`_item_savepoint`).
  - Smart contract condition fulfillment + Escrow settlement + Tamper-evident audit chain logging.

### Tier 4: Real-World Lifecycle Workloads (`tests/e2e/test_tier4_real_world.py`)
- **Focus**: Multi-stage, end-to-end production simulation workflows.
- **Coverage**:
  - Complete 5-stage lifecycle flow:
    1. Client identity mTLS validation & RS256 JWT issuance with Ingress proxy token check.
    2. Bank transfer execution with deterministic row locking and idempotency replay.
    3. Digital Ruble smart contract execution under AST sandbox and escrow settlement.
    4. Bank End-of-Day (EOD) batch reconciliation with savepoint fault isolation.
    5. Mathematical audit chain verification validating 100% integrity across all stages.

---

## 3. Test Runner & Execution Guidelines

### Standard Runner Command
All tests MUST be executed with explicit timeout protection:

```powershell
pytest tests/e2e/ --timeout=30
```

To run the complete platform unit and integration test suite:

```powershell
pytest --timeout=30
```

### Safety and Isolation Controls
1. **Windows ProactorEventLoop Self-Pipe Emulation**:
   - In Python 3.14 on Windows, `BaseProactorEventLoop._make_self_pipe` creates internal socketpairs that block indefinitely inside restricted sandbox firewalls.
   - `tests/e2e/conftest.py` installs a deterministic self-pipe mock for Windows environments, enabling instantaneous asyncio execution without network deadlocks.
2. **Deterministic Cryptographic Material**:
   - `pki_ca_authority` and `cert_factory` fixtures generate ephemeral in-memory RSA-2048 keys and X.509 certificates conforming to RFC 5280.
   - Private keys are stored in memory or isolated temporary directories and destroyed on test completion.
3. **Database and Storage Isolation**:
   - All tests run against asynchronous in-memory SQLite engines or SQLAlchemy `AsyncMock` sessions with nested transaction support (`session.begin_nested()`).
   - Redis operations are backed by `E2ERedisMock` implementing TTL expiration, NX key locks, and pubsub channels without requiring an external Redis daemon.
4. **Proxy Internal Secret**:
   - In automated test suites, `PROXY_INTERNAL_SECRET` is configured to `test-e2e-proxy-secret-token-32b!`.
   - Reverse proxy headers (`X-Proxy-Secret`) are validated using `hmac.compare_digest` to prevent side-channel timing attacks.
