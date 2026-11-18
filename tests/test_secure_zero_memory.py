"""Проверяет затирание буфера, в котором лежал ключ."""

from src.common.crypto.zeroize import secure_zero_memory


def test_secure_zero_memory_wipes_writable_buffers():
    """Проверяет, что изменяемый буфер, представление и список становятся нулями."""
    secret = bytearray(b"secret-key-material")
    secure_zero_memory(secret)
    assert secret == bytearray(len(b"secret-key-material"))

    viewed = bytearray(b"another-secret")
    secure_zero_memory(memoryview(viewed))
    assert viewed == bytearray(len(b"another-secret"))

    numbers = [1, 2, 3]
    secure_zero_memory(numbers)
    assert numbers == [0, 0, 0]


def test_secure_zero_memory_does_not_touch_readonly_view_or_none():
    """Проверяет, что неизменяемое представление и пустое значение остаются на месте."""
    raw = b"keep-me"
    secure_zero_memory(memoryview(raw))
    assert raw == b"keep-me"
    secure_zero_memory(None)
