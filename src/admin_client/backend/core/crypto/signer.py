"""
Единый фасад криптографической подписи и проверки квитанций для Admin Client (Zero-Trust Signer).
"""

import os
from pathlib import Path
import time
import datetime
import uuid
import json
import base64
import hashlib
import re
from typing import Optional, Any, Union

from cryptography.hazmat.primitives.asymmetric import rsa, ec, padding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography import x509
from cryptography.exceptions import InvalidSignature

from .canonical import compute_rag_canonical_digest, compute_receipt_canonical_bytes
from .hardware_bridge import HardwareSigningBridge, SimulatedTPMBridge
from .software_bridge import SoftwareSigningBridge


class ZeroTrustClientSigner:
    """
    Управляет клиентской криптографической подписью запросов к RAG API и валидацией квитанций сервера.
    Система функционирует строго в аппаратном режиме:
    - 'hardware': Строго аппаратный TPM 2.0 (Windows CNG). Используется по умолчанию.
    Для изолированного тестирования в CI/тестовых стендах доступны специализированные мосты:
    - 'simulated_tpm': Эмуляция TPM 2.0 / Windows Hello с подтверждением биометрии;
    - 'software': Программные ключи (admin_client.key / admin_client.crt).

    Внимание: режим 'auto' полностью исключен из соображений Zero-Trust безопасности.
    """

    def __init__(
        self,
        mode: str = "hardware",
        cert_path: Optional[str] = None,
        key_path: Optional[str] = None,
        ca_cert_path: Optional[str] = None,
        key_pem: Optional[str] = None,
        cert_pem: Optional[str] = None,
        ca_cert_pem: Optional[str] = None,
        signing_scheme: str = "PSS",
        hardware_key_name: str = "FinancialAI_Admin_TPM_Key",
        require_biometrics: bool = False
    ):
        if mode == "auto":
            raise ValueError(
                "Режим 'auto' запрещен спецификацией безопасности Zero-Trust: "
                "система работает строго в режиме 'hardware' (TPM 2.0)"
            )

        self.mode = mode
        self.signing_scheme = signing_scheme
        self.cert_path = cert_path
        self.key_path = key_path
        self.ca_cert_path = ca_cert_path
        self._ca_cert_pem = ca_cert_pem

        self._active_bridge: Union[HardwareSigningBridge, SimulatedTPMBridge, SoftwareSigningBridge]

        if mode == "hardware":
            if not HardwareSigningBridge.is_tpm_available():
                raise RuntimeError("Аппаратный криптомодуль TPM 2.0 недоступен на данном хосте")
            self._active_bridge = HardwareSigningBridge(
                key_name=hardware_key_name,
                require_biometrics=require_biometrics,
                cert_path=cert_path,
                cert_pem=cert_pem,
                ca_cert_path=ca_cert_path
            )
        elif mode == "simulated_tpm":
            self._active_bridge = SimulatedTPMBridge(
                key_path=key_path,
                key_pem=key_pem,
                cert_path=cert_path,
                cert_pem=cert_pem,
                ca_cert_path=ca_cert_path,
                ca_cert_pem=ca_cert_pem
            )
        elif mode == "software":
            self._active_bridge = SoftwareSigningBridge(
                cert_path=cert_path,
                key_path=key_path,
                key_pem=key_pem,
                cert_pem=cert_pem,
                ca_cert_path=ca_cert_path,
                ca_cert_pem=ca_cert_pem
            )
        else:
            raise ValueError(
                f"Недопустимый режим криптографической подписи '{mode}'. "
                "Система функционирует строго в режиме 'hardware'."
            )

    def get_certificate_pem(self) -> str:
        """Возвращает клиентский X.509 сертификат в формате PEM."""
        return self._active_bridge.get_certificate_pem()

    def get_public_key_pem(self) -> str:
        """Возвращает открытый ключ клиента в формате PEM."""
        return self._active_bridge.get_public_key_pem()

    def get_key_fingerprint(self) -> str:
        """Вычисляет SHA-256 отпечаток открытого ключа клиента."""
        pub_pem = self.get_public_key_pem()
        der = serialization.load_pem_public_key(pub_pem.encode("utf-8")).public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return f"sha256:{hashlib.sha256(der).hexdigest()}"

    def sign(self, canonical_bytes: bytes) -> str:
        """Подписывает канонические байты и возвращает подпись в Base64."""
        return self._active_bridge.sign_digest(canonical_bytes, scheme=self.signing_scheme)

    def create_rag_request_headers(
        self,
        body_bytes: bytes,
        nonce: Optional[str] = None,
        timestamp: Optional[int] = None
    ) -> dict[str, str]:
        """
        Формирует защитные заголовки Zero-Trust для отправки в POST /api/v1/rag/documents:
        - X-Timestamp (Unix Epoch);
        - X-Nonce (одноразовый случайный идентификатор);
        - X-Signature (подпись канонического дайджеста);
        - X-Cert (клиентский сертификат).
        """
        req_ts = timestamp if timestamp is not None else int(time.time())
        req_nonce = nonce if nonce is not None else uuid.uuid4().hex

        canonical = compute_rag_canonical_digest(
            timestamp=req_ts,
            nonce=req_nonce,
            body_bytes=body_bytes
        )

        sig_b64 = self.sign(canonical)

        return {
            "X-Timestamp": str(req_ts),
            "X-Nonce": req_nonce,
            "X-Signature": sig_b64,
            "X-Cert": self.get_certificate_pem(),
        }

    def _resolve_trusted_root_ca_pem(self) -> Optional[str]:
        """Разрешает PEM-строку доверенного Root CA из параметров, переменных окружения, диска или Windows Store."""
        if self._ca_cert_pem and self._ca_cert_pem.strip():
            return self._ca_cert_pem.strip()

        if self.ca_cert_path and os.path.exists(self.ca_cert_path):
            try:
                with open(self.ca_cert_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if "BEGIN CERTIFICATE" in content:
                        return content
            except Exception:
                pass

        env_ca = os.getenv("CA_CERT_PATH", "").strip() or os.getenv("ROOT_CA_CERT_PATH", "").strip()
        if env_ca and os.path.exists(env_ca):
            try:
                with open(env_ca, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if "BEGIN CERTIFICATE" in content:
                        return content
            except Exception:
                pass

        for cand in ["./certs/ca.crt", "/certs/ca.crt", "certs/ca.crt"]:
            if os.path.exists(cand):
                try:
                    with open(cand, "r", encoding="utf-8") as f:
                        content = f.read().strip()
                        if "BEGIN CERTIFICATE" in content:
                            return content
                except Exception:
                    pass

        try:
            repo_certs = Path(__file__).resolve().parents[5] / "certs" / "ca.crt"
            if repo_certs.exists():
                with open(repo_certs, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if "BEGIN CERTIFICATE" in content:
                        return content
        except Exception:
            pass

        try:
            from .cert_validator import find_root_ca_in_windows_store
            win_ca = find_root_ca_in_windows_store()
            if win_ca:
                return win_ca[0]
        except Exception:
            pass

        return None

    def verify_server_receipt(
        self,
        receipt_data: dict[str, Any],
        server_signature_b64: str,
        trusted_server_cert_or_pubkey: Optional[str] = None
    ) -> bool:
        """
        Криптографически проверяет подлинность квитанции сервера (AckReceipt).
        
        Args:
            receipt_data: Словарь с данными квитанции.
            server_signature_b64: Цифровая подпись сервера в Base64.
            trusted_server_cert_or_pubkey: Открытый ключ или сертификат сервера/Root CA.
        """
        if not server_signature_b64 or not server_signature_b64.strip():
            return False

        pub_key_source = None
        if trusted_server_cert_or_pubkey:
            candidate = trusted_server_cert_or_pubkey.strip()
            if not candidate.startswith("-----") and len(candidate) > 64:
                try:
                    decoded = base64.b64decode(candidate).decode("utf-8")
                    if "-----BEGIN" in decoded:
                        candidate = decoded
                except Exception:
                    pass
            pub_key_source = candidate

        # Если явный ключ/сертификат не передан, ищем сертификат или открытый ключ сервера на диске
        if not pub_key_source:
            server_candidates = [
                os.getenv("SERVER_CERT_PATH", "").strip(),
                os.getenv("JWT_PUBLIC_KEY_PATH", "").strip(),
                "/certs/admin_server.crt",
                "./certs/admin_server.crt",
                "certs/admin_server.crt",
                "/certs/admin_server.pub",
                "./certs/admin_server.pub",
                "certs/admin_server.pub",
            ]
            try:
                repo_certs = Path(__file__).resolve().parents[5] / "certs"
                server_candidates.append(str(repo_certs / "admin_server.crt"))
                server_candidates.append(str(repo_certs / "admin_server.pub"))
            except Exception:
                pass

            for cand in server_candidates:
                if cand and os.path.exists(cand):
                    try:
                        with open(cand, "r", encoding="utf-8") as f:
                            content = f.read().strip()
                            if "BEGIN CERTIFICATE" in content or "BEGIN PUBLIC KEY" in content or "BEGIN RSA PUBLIC KEY" in content:
                                pub_key_source = content
                                break
                    except Exception:
                        pass

        # Если сертификат сервера все еще не найден, запасной вариант - доверенный Root CA
        if not pub_key_source:
            pub_key_source = self._resolve_trusted_root_ca_pem()

        if not pub_key_source:
            return False

        canonical_receipt = compute_receipt_canonical_bytes(receipt_data)

        try:
            raw_sig = base64.b64decode(server_signature_b64.strip())
            pem_bytes = pub_key_source.strip().encode("utf-8")

            pub_key = None
            if b"-----BEGIN CERTIFICATE-----" in pem_bytes:
                cert_blocks = re.findall(
                    r"-----BEGIN CERTIFICATE-----[\s\S]+?-----END CERTIFICATE-----",
                    pub_key_source
                )
                if not cert_blocks:
                    return False

                parsed_certs = []
                for block in cert_blocks:
                    try:
                        parsed_certs.append(x509.load_pem_x509_certificate(block.encode("utf-8")))
                    except Exception:
                        return False

                leaf_cert = parsed_certs[0]

                # Разрешаем доверенный Root CA
                root_ca_pem = self._resolve_trusted_root_ca_pem()
                if not root_ca_pem:
                    return False

                try:
                    root_cert = x509.load_pem_x509_certificate(root_ca_pem.strip().encode("utf-8"))
                except Exception:
                    return False

                now = datetime.datetime.now(datetime.timezone.utc)
                if now < root_cert.not_valid_before_utc or now > root_cert.not_valid_after_utc:
                    return False

                for c in parsed_certs:
                    if now < c.not_valid_before_utc or now > c.not_valid_after_utc:
                        return False

                # Проверка на нелегитимный самоподписанный сертификат
                if leaf_cert.issuer == leaf_cert.subject and leaf_cert.subject != root_cert.subject:
                    return False

                def _verify_cert_sig(child, issuer_key):
                    if isinstance(issuer_key, rsa.RSAPublicKey):
                        issuer_key.verify(
                            child.signature,
                            child.tbs_certificate_bytes,
                            padding.PKCS1v15(),
                            child.signature_hash_algorithm
                        )
                        return True
                    elif isinstance(issuer_key, ec.EllipticCurvePublicKey):
                        issuer_key.verify(
                            child.signature,
                            child.tbs_certificate_bytes,
                            ec.ECDSA(child.signature_hash_algorithm)
                        )
                        return True
                    elif hasattr(issuer_key, "verify"):
                        issuer_key.verify(
                            child.signature,
                            child.tbs_certificate_bytes,
                            child.signature_hash_algorithm
                        )
                        return True
                    return False

                current = leaf_cert
                intermediates = parsed_certs[1:]

                try:
                    while current.issuer != root_cert.subject:
                        matching_issuer = next(
                            (ic for ic in intermediates if ic.subject == current.issuer),
                            None
                        )
                        if not matching_issuer:
                            return False

                        if not _verify_cert_sig(current, matching_issuer.public_key()):
                            return False

                        current = matching_issuer

                    if not _verify_cert_sig(current, root_cert.public_key()):
                        return False
                except Exception:
                    return False

                pub_key = leaf_cert.public_key()
            elif b"-----BEGIN PUBLIC KEY-----" in pem_bytes or b"-----BEGIN RSA PUBLIC KEY-----" in pem_bytes:
                pub_key = serialization.load_pem_public_key(pem_bytes)

            if pub_key is None:
                return False

            if isinstance(pub_key, rsa.RSAPublicKey):
                # Пробуем RSA-PSS (Max Salt)
                try:
                    pub_key.verify(
                        raw_sig,
                        canonical_receipt,
                        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
                        hashes.SHA256()
                    )
                    return True
                except InvalidSignature:
                    pass

                # Пробуем RSA-PSS (Digest Salt)
                try:
                    pub_key.verify(
                        raw_sig,
                        canonical_receipt,
                        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                        hashes.SHA256()
                    )
                    return True
                except InvalidSignature:
                    pass

                # Пробуем PKCS#1 v1.5
                try:
                    pub_key.verify(
                        raw_sig,
                        canonical_receipt,
                        padding.PKCS1v15(),
                        hashes.SHA256()
                    )
                    return True
                except InvalidSignature:
                    pass

            elif isinstance(pub_key, ec.EllipticCurvePublicKey):
                try:
                    pub_key.verify(raw_sig, canonical_receipt, ec.ECDSA(hashes.SHA256()))
                    return True
                except InvalidSignature:
                    pass

            return False
        except Exception:
            return False

    def submit_zero_trust_document(
        self,
        url: str,
        jwt_token: str,
        content: str,
        collection_name: str = "financial_kb",
        metadata: Optional[dict[str, Any]] = None,
        document_title: Optional[str] = None,
        session: Optional[Any] = None
    ) -> dict[str, Any]:
        """
        Отправляет документ в защищенный эндпоинт RAG (POST /api/v1/rag/documents) с цифровой подписью.
        """
        import requests
        http_client = session or requests.Session()

        meta = metadata or {}
        doc_title = (
            document_title
            or meta.get("system_name")
            or meta.get("short_name")
            or meta.get("filename")
            or "Regulatory Document"
        )
        payload = {
            "document_title": str(doc_title),
            "collection_name": collection_name,
            "content": content,
            "metadata": meta
        }
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        zt_headers = self.create_rag_request_headers(body_bytes)
        headers = {
            "Authorization": f"Bearer {jwt_token.replace('Bearer ', '').strip()}",
            "Content-Type": "application/json",
            **zt_headers
        }
        resp = http_client.post(url, data=body_bytes, headers=headers, timeout=(5, 120))
        resp.raise_for_status()
        resp_data = resp.json()

        server_sig = resp_data.get("signature") or resp.headers.get("X-Admin-Signature", "")
        receipt_verified = False
        if server_sig:
            server_cert = resp_data.get("server_cert") or resp.headers.get("X-Server-Cert")
            receipt_verified = self.verify_server_receipt(
                resp_data, server_sig, trusted_server_cert_or_pubkey=server_cert
            )
        resp_data["receipt_verified"] = receipt_verified
        return resp_data

