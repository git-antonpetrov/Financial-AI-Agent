"""
Модуль клиентской криптографии Zero-Trust (TPM 2.0 / Windows Hello & Software Signer).
"""

from .canonical import compute_rag_canonical_digest, compute_receipt_canonical_bytes
from .hardware_bridge import HardwareSigningBridge, SimulatedTPMBridge
from .software_bridge import SoftwareSigningBridge
from .signer import ZeroTrustClientSigner

__all__ = [
    "compute_rag_canonical_digest",
    "compute_receipt_canonical_bytes",
    "HardwareSigningBridge",
    "SimulatedTPMBridge",
    "SoftwareSigningBridge",
    "ZeroTrustClientSigner",
]
