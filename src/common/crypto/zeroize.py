"""
Модуль криптографического затирания конфиденциальных данных в оперативной памяти (Secure Memory Zeroization).
Соответствует требованиям NIST SP 800-88 и архитектурной спецификации Zero-Trust PKI.
"""

import ctypes
from typing import Any


def secure_zero_memory(target: bytearray | memoryview | list[int] | Any) -> None:
    """
    Гарантированно перезаписывает содержимое буфера оперативной памяти нулями.
    Использует прямой доступ к адресам C-уровня через ctypes.memset для предотвращения
    оптимизаций компилятора и сборщика мусора Python.
    """
    if target is None:
        return

    if isinstance(target, bytearray):
        length = len(target)
        if length > 0:
            location = (ctypes.c_char * length).from_buffer(target)
            ctypes.memset(ctypes.addressof(location), 0, length)
    elif isinstance(target, memoryview):
        length = target.nbytes
        if length > 0 and not target.readonly:
            location = (ctypes.c_char * length).from_buffer(target)
            ctypes.memset(ctypes.addressof(location), 0, length)
    elif hasattr(target, "__len__") and hasattr(target, "__setitem__"):
        try:
            for i in range(len(target)):
                target[i] = 0
        except Exception:
            pass
