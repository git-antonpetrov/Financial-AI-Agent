# Project: Financial-AI-Agent Security & Architecture Audit

## Architecture
Financial-AI-Agent is a high-assurance financial platform designed for multiagent simulation and autonomous operations.
The architecture is structured into distinct trust zones with strict zero-trust network boundaries:
1. **Public Edge Ingress**: Caddy reverse proxy on external network (`front_net`), exposing only ports 80/443. Handles TLS termination and forwards traffic to the internal network.
2. **Administration & Control Plane**: Admin Server (FastAPI, SQLAlchemy asyncpg, PostgreSQL, Redis) enforcing RS256 JWT, RFC 6238 2FA TOTP, SHA-256 hash-chain audit logging, and `X-Proxy-Secret` validation.
3. **Cryptographic Identity Service (PKI)**: Root CA (RSA-4096, AES-256 encrypted), intermediate cert issuance, CRL distribution, and Redis pubsub runtime revocation listeners.
4. **Vector Storage & RAG Pipeline**: Vector Worker, ChromaDB with AES-256-GCM envelope encryption, MinIO document storage, mutual X.509 auth with HMAC signatures and replay nonces.
5. **Financial Simulation Engine**: Banking accounts and transfer routers, Digital Ruble ledger, investment tracking, and EOD batch reconciliation workers using PostgreSQL advisory locks.
6. **Smart Contract Sandbox**: AST-level security isolation (no loops, imports, or dunder attributes), instruction-stepped runtime execution limits, and strict escrow-bounded token operations.
7. **External Gateways**: Cloudflare Worker reverse proxies interfacing with Google Vertex AI / Gemini and Tavily Search APIs.

## Feature Inventory
Every feature, vulnerability, and architectural invariant identified during Phase 0 Survey is mapped here:
| # | Feature / Finding | Description | Milestone | Source |
|---|-------------------|-------------|-----------|--------|
| 1 | Private Key Zeroization | `crud.py` zeros public key copy instead of private key bytes (NIST SP 800-88) | M1 | survey_core_platform |
| 2 | PKI CRL & Revocation | Validate RFC 5280 CRL generation and Redis pubsub revocation synchronization | M1 | survey_core_platform |
| 3 | Caddy Ingress TLS Configuration | Missing `tls internal` in active site blocks and missing server cert container mount | M2 | survey_core_platform |
| 4 | Proxy Secret Hardening | Remove hardcoded fallback token and prevent bypass when `X-Proxy-Secret` env is empty | M2 | survey_core_platform |
| 5 | Docker Volume Isolation | Eliminate shared `certs_data:/certs:ro` mount across DB, Redis, MinIO (least privilege) | M2 | survey_core_platform |
| 6 | Bank Transfer Idempotency | Add mandatory `X-Idempotency-Key` handling in banking API and transaction store | M3 | survey_financial_ai |
| 7 | Concurrency Race Hardening | Ensure destination account lookup and balance checks occur inside `with_for_update()` lock | M3 | survey_financial_ai |
| 8 | EOD Batch Fault Isolation | Add nested savepoints (`session.begin_nested()`) to Bank and Invest EOD workers | M3 | survey_financial_ai |
| 9 | Gateway Timeout & CORS Hardening | Add `AbortSignal.timeout(10000)`, restrict CORS origin, and enforce manual redirect | M4 | survey_financial_ai |
| 10 | Smart Contract AST Isolation Verification | Empirical and static verification of sandbox loop-free and import-free invariant | M5 | survey_financial_ai |
| 11 | RAG Envelope Encryption Verification | Verification of AES-256-GCM encryption of vector chunks in ChromaDB | M5 | survey_financial_ai |
| 12 | End-to-End Test Suite Pass | 100% pass across all 4 tiers of opaque-box E2E test suite + Tier 5 adversarial hardening | M5 | original_request |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | PKI & Key Zeroization Remediation | `src/admin_server/fastapi/db/crud.py`, PKI lifecycle verification | none | DONE |
| M2 | Ingress, Ingress Security & Volume Isolation | `src/caddy/Caddyfile`, `src/admin_server/fastapi/security.py`, `docker-compose.yml` | M1 | DONE |
| M3 | Financial Idempotency & Concurrency Hardening | `src/simulations/api/routers/bank.py`, models, and EOD batch workers | none | DONE |
| M4 | External API Gateway Hardening | `src/gateways/gemini-gate.js`, `tavily-gate.js` | none | DONE |
| M5 | Final Milestone: Full E2E & Adversarial Hardening | Pass 100% E2E test suite (Tiers 1-4) + Tier 5 adversarial penetration hardening | M1, M2, M3, M4, TEST_READY | DONE |

## Interface Contracts
### Admin Server ↔ Caddy Ingress
- Protocol: HTTP over internal bridge network (`admin_net`).
- Mandatory Header: `X-Proxy-Secret: <cryptographically-secure-random-token>`.
- Rejection: HTTP 403 Forbidden on missing, empty, or mismatched secret.

### Banking API ↔ Clients
- Header: `X-Idempotency-Key: <uuid4>`.
- Guarantee: Exactly-once execution for transfer requests. Duplicate keys return cached original transaction response without balance modification.

### Microservices ↔ PKI Certificate Storage
- Isolation: Dedicated read-only volumes per service (`certs_admin`, `certs_bank`, `certs_db`, `certs_redis`, `certs_minio`).
- Access: Only the certificate and private key assigned to that specific service are readable.

## Code Layout
- `src/pki/`: PKI certificate authority, key generation, and revocation
- `src/caddy/`: Caddy reverse proxy configuration and ingress rules
- `src/admin_server/`: Admin API, authentication, RBAC, audit logging, RAG vector worker
- `src/simulations/`: Banking simulation, Digital Ruble, investments, EOD workers, Smart Contract Sandbox
- `src/gateways/`: Cloudflare Worker API edge proxies (Gemini, Tavily)
- `scripts/`: Deployment, VDS security initialization, and volume setup scripts
- `tests/`: Pytest unit and integration test suite (49 modules, 399 tests)
- `tests/e2e/`: Opaque-box end-to-end verification test suite
