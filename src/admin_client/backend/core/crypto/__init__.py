"""
Модуль клиентской криптографии Zero-Trust (TPM 2.0 / Windows Hello & Software Signer).
"""

from .canonical import compute_rag_canonical_digest, compute_receipt_canonical_bytes
from .cert_validator import (
    CertificateMissingError,
    CertificateValidationError,
    KeyMissingError,
    WindowsCertificateStoreError,
    create_test_ca_and_client_cert,
    export_windows_cert_to_cache,
    find_cert_in_windows_store,
    find_certificate_file,
    find_client_cert_in_windows_store,
    find_root_ca_in_windows_store,
    get_windows_cert_store_help_message,
    resolve_and_validate_client_certificate,
)
from .hardware_bridge import HardwareSigningBridge, SimulatedTPMBridge
from .signer import ZeroTrustClientSigner
from .software_bridge import SoftwareSigningBridge

__all__ = [
    "CertificateMissingError",
    "CertificateValidationError",
    "HardwareSigningBridge",
    "KeyMissingError",
    "SimulatedTPMBridge",
    "SoftwareSigningBridge",
    "WindowsCertificateStoreError",
    "ZeroTrustClientSigner",
    "compute_rag_canonical_digest",
    "compute_receipt_canonical_bytes",
    "create_test_ca_and_client_cert",
    "export_windows_cert_to_cache",
    "find_cert_in_windows_store",
    "find_certificate_file",
    "find_client_cert_in_windows_store",
    "find_root_ca_in_windows_store",
    "get_windows_cert_store_help_message",
    "resolve_and_validate_client_certificate",
]
