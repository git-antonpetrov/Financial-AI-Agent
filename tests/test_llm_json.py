import os
import sys
from unittest.mock import MagicMock

# Ensure env vars
os.environ["ADMIN_PASSWORD"] = "test"
os.environ["JWT_SECRET_KEY"] = "test"
os.environ["POSTGRES_PASSWORD"] = "test"
for agent in ["DIGITAL", "MAIN", "BANK", "INVEST"]:
    os.environ[f"AGENT_{agent}_BOOTSTRAP_TOKEN"] = "test_token"

# Pre-import pip fastapi
import fastapi

# Mock heavy external services not installed in host test environment
for mod in ["redis", "minio", "litellm", "asyncpg"]:
    sys.modules[mod] = MagicMock()

sys.path.insert(0, os.path.abspath("src/admin_server"))
sys.path.insert(0, os.path.abspath("src/admin_server/fastapi"))

from server import extract_json_from_llm

def test_extract_json_markdown():
    text = "```json\n{\"system_name\": \"fz_115_01012024\", \"short_name\": \"fz_115\"}\n```"
    res = extract_json_from_llm(text)
    assert res == {"system_name": "fz_115_01012024", "short_name": "fz_115"}

def test_extract_json_with_think_and_text():
    text = "<think>Let me evaluate this act.</think>Here is the json:\n```\n{\"system_name\": \"fz_1\", \"short_name\": \"fz_1\"}\n```\nHope it helps!"
    res = extract_json_from_llm(text)
    assert res == {"system_name": "fz_1", "short_name": "fz_1"}

def test_extract_json_plain():
    text = "Based on analysis: {\"system_name\": \"fz_1\", \"short_name\": \"fz_1\"}"
    res = extract_json_from_llm(text)
    assert res == {"system_name": "fz_1", "short_name": "fz_1"}

def test_extract_json_list():
    text = "Repealed documents: [\"doc_1\", \"doc_2\"]"
    res = extract_json_from_llm(text)
    assert res == ["doc_1", "doc_2"]

def test_extract_json_empty_list():
    text = "No repealed documents: []"
    res = extract_json_from_llm(text)
    assert res == []

def test_get_client_ip_anti_spoofing():
    from server import get_client_ip
    
    # 1. Attacker sends spoofed header, Caddy appends real IP (203.0.113.195)
    req = MagicMock()
    req.headers = {"x-forwarded-for": "1.2.3.4, 10.0.0.1, 203.0.113.195"}
    req.client.host = "172.18.0.2"
    assert get_client_ip(req) == "203.0.113.195"

    # 2. Direct connection without proxy header
    req_direct = MagicMock()
    req_direct.headers = {}
    req_direct.client.host = "192.168.1.50"
    assert get_client_ip(req_direct) == "192.168.1.50"

    # 3. Invalid/malformed IP in header
    req_invalid = MagicMock()
    req_invalid.headers = {"x-forwarded-for": "invalid-ip"}
    req_invalid.client.host = "192.168.1.50"
    assert get_client_ip(req_invalid) == "192.168.1.50"

def test_create_access_token_jti_and_iat():
    from security import create_access_token, settings
    import jwt

    token = create_access_token({"sub": "admin"})
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])

    assert payload.get("sub") == "admin"
    assert "jti" in payload
    assert isinstance(payload["jti"], str)
    assert len(payload["jti"]) > 10
    assert "iat" in payload
    assert "exp" in payload
    assert payload["exp"] > payload["iat"]
