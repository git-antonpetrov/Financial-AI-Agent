"""
Модульные тесты для проверки аутентификации администратора и вызова API заявок агентов.
"""

import os
import jwt
import datetime
import urllib.request
import urllib.error
import io
import json
from unittest.mock import patch, MagicMock


def create_test_token(secret: str, sub: str = "admin", expires_in_days: int = 1) -> str:
    """Генерирует тестовый JWT токен с заданным секретом и сроком жизни."""
    payload = {
        "sub": sub,
        "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=expires_in_days)
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def test_admin_jwt_token_generation_and_decoding():
    """Проверяет корректность создания и декодирования JWT токена администратора."""
    jwt_secret = os.getenv("JWT_SECRET_KEY", "test_jwt_secret_key_minimum_32_bytes_long_12345")
    token = create_test_token(jwt_secret)
    
    assert isinstance(token, str)
    decoded = jwt.decode(token, jwt_secret, algorithms=["HS256"])
    assert decoded["sub"] == "admin"
    assert "exp" in decoded


def test_admin_jwt_token_expired():
    """Проверяет отклонение JWT токена с истекшим сроком действия."""
    jwt_secret = os.getenv("JWT_SECRET_KEY", "test_jwt_secret_key_minimum_32_bytes_long_12345")
    token = create_test_token(jwt_secret, expires_in_days=-1)
    
    try:
        jwt.decode(token, jwt_secret, algorithms=["HS256"])
        assert False, "Токен с истекшим сроком действия должен вызывать ошибку ExpiredSignatureError"
    except jwt.ExpiredSignatureError:
        pass


def test_agent_requests_api_call_success():
    """Проверяет успешное обращение к API заявок агентов с валидным Bearer-токеном."""
    jwt_secret = os.getenv("JWT_SECRET_KEY", "test_jwt_secret_key_minimum_32_bytes_long_12345")
    token = create_test_token(jwt_secret)
    target_url = "https://admin.fin-ai-agent.ru/api/agent-requests"
    
    req = urllib.request.Request(target_url, headers={"Authorization": f"Bearer {token}"})
    assert req.get_header("Authorization") == f"Bearer {token}"
    assert req.full_url == target_url

    mock_response_data = json.dumps([
        {
            "id": 1,
            "agent_name": "main",
            "document_name_ru": "Тестовый регламент",
            "status": "pending"
        }
    ]).encode("utf-8")

    mock_response = MagicMock()
    mock_response.read.return_value = mock_response_data
    mock_response.__enter__.return_value = mock_response

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        with urllib.request.urlopen(req) as res:
            data = json.loads(res.read().decode("utf-8"))
            assert len(data) == 1
            assert data[0]["agent_name"] == "main"
        mock_urlopen.assert_called_once_with(req)


def test_agent_requests_api_call_unauthorized():
    """Проверяет корректность обработки ошибки 401 Unauthorized при невалидном токене."""
    target_url = "https://admin.fin-ai-agent.ru/api/agent-requests"
    req = urllib.request.Request(target_url, headers={"Authorization": "Bearer invalid_token"})

    mock_http_error = urllib.error.HTTPError(
        url=target_url,
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=io.BytesIO(b'{"detail": "Could not validate credentials"}')
    )

    with patch("urllib.request.urlopen", side_effect=mock_http_error):
        try:
            urllib.request.urlopen(req)
            assert False, "Запрос без валидного токена должен приводить к HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 401
        finally:
            mock_http_error.close()

