"""
Модуль клиентской криптографии Zero-Trust (TPM 2.0 / Windows Hello & Software Signer).
"""

from .canonical import compute_rag_canonical_digest, compute_receipt_canonical_bytes
from .hardware_bridge import HardwareSigningBridge, SimulatedTPMBridge
from .software_bridge import SoftwareSigningBridge
from .signer import ZeroTrustClientSigner
from .cert_validator import (
    CertificateMissingError,
    WindowsCertificateStoreError,
    CertificateValidationError,
    KeyMissingError,
    find_certificate_file,
    find_cert_in_windows_store,
    find_root_ca_in_windows_store,
    find_client_cert_in_windows_store,
    export_windows_cert_to_cache,
    get_windows_cert_store_help_message,
    resolve_and_validate_client_certificate,
    create_test_ca_and_client_cert,
)

__all__ = [
    "compute_rag_canonical_digest",
    "compute_receipt_canonical_bytes",
    "HardwareSigningBridge",
    "SimulatedTPMBridge",
    "SoftwareSigningBridge",
    "ZeroTrustClientSigner",
    "CertificateMissingError",
    "WindowsCertificateStoreError",
    "CertificateValidationError",
    "KeyMissingError",
    "find_certificate_file",
    "find_cert_in_windows_store",
    "find_root_ca_in_windows_store",
    "find_client_cert_in_windows_store",
    "export_windows_cert_to_cache",
    "get_windows_cert_store_help_message",
    "resolve_and_validate_client_certificate",
    "create_test_ca_and_client_cert",
]
