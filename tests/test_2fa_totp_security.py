"""
Модульные и интеграционные тесты для проверки двухфакторной аутентификации 2FA (RFC 6238 TOTP):
- Генерация секрета и URI подключения аутентификатора;
- Валидация 6-значных одноразовых кодов через pyotp и pure-python RFC 6238 fallback;
- Защита от Replay Attack (блокировка повторного ввода одного кода);
- Интеграция с эндпоинтом /login: требование 2FA, отклонение невалидных кодов;
- Эндпоинты /api/auth/2fa/status и /api/auth/2fa/setup.
"""

import os
import sys
import time
import pytest
import pyotp
from unittest.mock import MagicMock

# Предварительный импорт fastapi до модификаций sys.path
import fastapi
from fastapi.testclient import TestClient

# Моки внешних сервисов
for mod in ["redis", "minio", "litellm", "asyncpg"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

# Настройка путей
sys.path.insert(0, os.path.abspath("src/admin_server"))
sys.path.insert(0, os.path.abspath("src/admin_server/fastapi"))

import security
from security import (
    settings,
    generate_totp_secret,
    get_totp_uri,
    verify_totp_code,
    create_access_token,
    get_password_hash,
)


class InMemoryRedisMock:
    """Имитатор Redis для тестирования 2FA, черного списка и защиты от replay-атак."""
    def __init__(self):
        self.store = {}

    def exists(self, key: str) -> bool:
        return key in self.store

    def get(self, key: str):
        return self.store.get(key)

    def setex(self, key: str, ttl: int, value: str):
        self.store[key] = value

    def delete(self, key: str):
        self.store.pop(key, None)

    def incr(self, key: str):
        val = int(self.store.get(key, 0)) + 1
        self.store[key] = str(val)
        return val

    def expire(self, key: str, ttl: int):
        pass


@pytest.fixture(autouse=True)
def setup_mock_redis(monkeypatch):
    """Изолирует хранилище Redis для каждого теста."""
    mock_redis = InMemoryRedisMock()
    monkeypatch.setattr(security, "redis_blacklist", mock_redis)
    import server
    monkeypatch.setattr(server, "redis_client", mock_redis)
    return mock_redis


def test_generate_and_verify_totp_code():
    """Проверяет генерацию секрета, вычисление и успешную проверку кода TOTP."""
    secret = generate_totp_secret()
    assert len(secret) >= 16

    totp = pyotp.TOTP(secret)
    current_code = totp.now()

    assert len(current_code) == 6
    assert current_code.isdigit()
    assert verify_totp_code(secret, current_code) is True


def test_verify_totp_invalid_inputs():
    """Проверяет отклонение некорректных форматов кода и неверных значений."""
    secret = generate_totp_secret()

    assert verify_totp_code(secret, "") is False
    assert verify_totp_code(secret, "12345") is False
    assert verify_totp_code(secret, "1234567") is False
    assert verify_totp_code(secret, "abcdef") is False
    assert verify_totp_code("", "123456") is False
    assert verify_totp_code(secret, "000000") is False


def test_totp_window_drift():
    """Проверяет допустимый дрейф времени (окно ±30 сек) и блокировку устаревших кодов."""
    secret = generate_totp_secret()
    totp = pyotp.TOTP(secret)

    # Текущий код валиден
    now = time.time()
    code_now = totp.at(now)
    assert verify_totp_code(secret, code_now) is True

    # Код со смещением +25 сек (в пределах 1 шага) валиден
    code_slight_drift = totp.at(now + 25)
    assert verify_totp_code(secret, code_slight_drift) is True

    # Код из далекого прошлого (10 минут назад) должен быть отклонен
    code_stale = totp.at(now - 600)
    assert verify_totp_code(secret, code_stale) is False


def test_pure_python_fallback_verification(monkeypatch):
    """Проверяет работу fallback-реализации RFC 6238 при отсутствии библиотеки pyotp."""
    secret = generate_totp_secret()
    totp = pyotp.TOTP(secret)
    code = totp.now()

    # Временно скрываем pyotp в security
    monkeypatch.setattr(security, "pyotp", None)

    # Верификация должна успешно отработать через стандартный алгоритм HMAC-SHA1
    assert verify_totp_code(secret, code) is True
    assert verify_totp_code(secret, "999999") is False


def test_login_enforces_2fa_and_rejects_missing_or_bad_code(monkeypatch):
    """Интеграционный тест: требование 2FA при входе администратора."""
    from server import app
    client = TestClient(app)

    test_password = "SecurePassword123!"
    test_totp_secret = pyotp.random_base32()

    # Настраиваем сервер с паролем и активным 2FA секретом
    monkeypatch.setattr(settings, "ADMIN_PASSWORD_HASH", get_password_hash(test_password))
    monkeypatch.setattr(settings, "ADMIN_TOTP_SECRET", test_totp_secret)
    monkeypatch.setattr(settings, "REQUIRE_2FA", True)

    # 1. Запрос на вход без 2FA кода -> 401 и заголовок X-2FA-Required
    res_no_otp = client.post("/login", data={"username": "admin", "password": test_password})
    assert res_no_otp.status_code == 401
    assert "2FA code required" in res_no_otp.json().get("detail", "")
    assert res_no_otp.headers.get("x-2fa-required") == "true"

    # 2. Запрос с неверным 2FA кодом -> 401
    res_bad_otp = client.post(
        "/login",
        data={"username": "admin", "password": test_password, "otp_code": "000000"}
    )
    assert res_bad_otp.status_code == 401
    assert "Invalid 2FA TOTP code" in res_bad_otp.json().get("detail", "")

    # 3. Запрос с валидным 2FA кодом -> 200 и выдача access + refresh токенов
    valid_code = pyotp.TOTP(test_totp_secret).now()
    res_good_otp = client.post(
        "/login",
        data={"username": "admin", "password": test_password, "otp_code": valid_code}
    )
    assert res_good_otp.status_code == 200
    data = res_good_otp.json()
    assert "access_token" in data
    assert "refresh_token" in data


def test_login_totp_replay_attack_blocked(monkeypatch):
    """Проверяет защиту от Replay Attack: один и тот же код нельзя применить дважды."""
    from server import app
    client = TestClient(app)

    test_password = "SecurePassword123!"
    test_totp_secret = pyotp.random_base32()

    monkeypatch.setattr(settings, "ADMIN_PASSWORD_HASH", get_password_hash(test_password))
    monkeypatch.setattr(settings, "ADMIN_TOTP_SECRET", test_totp_secret)
    monkeypatch.setattr(settings, "REQUIRE_2FA", True)

    valid_code = pyotp.TOTP(test_totp_secret).now()

    # Первая авторизация успешна
    res1 = client.post(
        "/login",
        data={"username": "admin", "password": test_password, "otp_code": valid_code}
    )
    assert res1.status_code == 200

    # Вторая попытка с ТЕМ ЖЕ кодом блокируется как повтор
    res2 = client.post(
        "/login",
        data={"username": "admin", "password": test_password, "otp_code": valid_code}
    )
    assert res2.status_code == 401
    assert "already been used" in res2.json().get("detail", "")


def test_2fa_status_and_setup_endpoints(monkeypatch):
    """Проверяет эндпоинты /api/auth/2fa/status и /api/auth/2fa/setup."""
    from server import app
    client = TestClient(app)

    # 1. Проверяем статус при отключенном 2FA
    monkeypatch.setattr(settings, "ADMIN_TOTP_SECRET", "")
    monkeypatch.setattr(settings, "REQUIRE_2FA", False)
    res_status_off = client.get("/api/auth/2fa/status")
    assert res_status_off.status_code == 200
    assert res_status_off.json()["enabled"] is False

    # 2. Проверяем статус при включенном 2FA
    monkeypatch.setattr(settings, "ADMIN_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    res_status_on = client.get("/api/auth/2fa/status")
    assert res_status_on.status_code == 200
    assert res_status_on.json()["enabled"] is True

    # 3. Эндпоинт генерации нового секрета требует токен администратора
    res_setup_unauth = client.post("/api/auth/2fa/setup")
    assert res_setup_unauth.status_code == 401

    admin_token = create_access_token({"sub": "admin"})
    res_setup_auth = client.post(
        "/api/auth/2fa/setup",
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert res_setup_auth.status_code == 200
    setup_data = res_setup_auth.json()
    assert "secret" in setup_data
    assert "provisioning_uri" in setup_data
    assert "otpauth://totp/" in setup_data["provisioning_uri"]
    assert "qr_svg" in setup_data
    assert "<svg" in setup_data["qr_svg"]

    # 4. GET /api/auth/2fa/setup возвращает данные текущего секрета
    res_setup_get = client.get(
        "/api/auth/2fa/setup",
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert res_setup_get.status_code == 200
    get_data = res_setup_get.json()
    assert get_data["secret"] == "JBSWY3DPEHPK3PXP"
    assert "<svg" in get_data["qr_svg"]


def test_2fa_verify_test_endpoint():
    """Проверяет эндпоинт проверки кода при начальной привязке аутентификатора."""
    from server import app
    client = TestClient(app)
    admin_token = create_access_token({"sub": "admin"})

    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    valid_code = totp.now()

    # Корректный код подтверждается
    res_valid = client.post(
        "/api/auth/2fa/verify-test",
        headers={"Authorization": f"Bearer {admin_token}"},
        data={"code": valid_code, "secret": secret}
    )
    assert res_valid.status_code == 200
    assert res_valid.json()["status"] == "ok"

    # Неверный код отклоняется
    res_invalid = client.post(
        "/api/auth/2fa/verify-test",
        headers={"Authorization": f"Bearer {admin_token}"},
        data={"code": "000000", "secret": secret}
    )
    assert res_invalid.status_code == 400
    assert "Invalid verification code" in res_invalid.json()["detail"]

