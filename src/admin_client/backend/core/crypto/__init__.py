"""
Модуль клиентской криптографии Zero-Trust (TPM 2.0 / Windows Hello & Software Signer).
"""

from .canonical import compute_rag_canonical_digest, compute_receipt_canonical_bytes
from .cert_validator import (
    CertificateMissingError,
    CertificateValidationError,
    KeyMissingError,
    WindowsCertificateStoreError,
    export_windows_cert_to_cache,
    find_cert_in_windows_store,
    find_certificate_file,
    find_client_cert_in_windows_store,
    find_root_ca_in_windows_store,
    get_windows_cert_store_help_message,
    resolve_and_validate_client_certificate,
)
from .hardware_bridge import HardwareSigningBridge
from .signer import ZeroTrustClientSigner

__all__ = [
    "CertificateMissingError",
    "CertificateValidationError",
    "HardwareSigningBridge",
    "KeyMissingError",
    "WindowsCertificateStoreError",
    "ZeroTrustClientSigner",
    "compute_rag_canonical_digest",
    "compute_receipt_canonical_bytes",
    "export_windows_cert_to_cache",
    "find_cert_in_windows_store",
    "find_certificate_file",
    "find_client_cert_in_windows_store",
    "find_root_ca_in_windows_store",
    "get_windows_cert_store_help_message",
    "resolve_and_validate_client_certificate",
]
