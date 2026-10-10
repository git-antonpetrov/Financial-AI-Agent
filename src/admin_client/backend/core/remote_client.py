"""
Модуль доверенного сетевого клиента и управления сессией администратора (RemoteAdminClient / BFF Gateway).
Инкапсулирует защищенную связь с удаленным Admin Server через Caddy по mTLS,
автоматическую ротацию токенов (Refresh Token Rotation), управление учетными записями и сессией.
Исключает утечку секретов, mTLS-протоколов и бизнес-логики во фронтенд.
"""

import os
import sys
import threading
from typing import Optional, Dict, Any, List
import requests

try:
    from src.admin_client.backend.core.crypto.cert_validator import (
        find_root_ca_in_windows_store,
        find_client_cert_in_windows_store,
        export_windows_cert_to_cache,
        find_certificate_file,
    )
except ImportError:
    try:
        from core.crypto.cert_validator import (
            find_root_ca_in_windows_store,
            find_client_cert_in_windows_store,
            export_windows_cert_to_cache,
            find_certificate_file,
        )
    except ImportError:
        find_root_ca_in_windows_store = None
        find_client_cert_in_windows_store = None
        export_windows_cert_to_cache = None
        find_certificate_file = None


class RemoteClientError(Exception):
    """Базовое исключение для ошибок связи с удаленным сервером."""
    pass


class AuthenticationError(RemoteClientError):
    """Исключение при ошибке аутентификации (неверный пароль, 2FA код или сессия)."""
    pass


class SessionExpiredError(RemoteClientError):
    """Исключение, когда сессия администратора окончательно истекла и требуется повторный вход."""
    pass


class RemoteAdminClient:
    """
    Единый доверенный клиент управления взаимодействием с сервером финансового агента (BFF).
    Хранит сессию в оперативной памяти процесса Python, автоматически применяет сертификаты mTLS
    из Windows Certificate Store / диска и прозрачно обновляет токены доступа.
    """

    def __init__(
        self,
        server_url: Optional[str] = None,
        ca_cert_path: Optional[str] = None,
        client_cert_path: Optional[str] = None,
        client_key_path: Optional[str] = None,
    ):
        self.server_url = server_url.rstrip("/") if server_url else ""
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.username: Optional[str] = None
        self.role: Optional[str] = None

        self.ca_cert_path = ca_cert_path
        self.client_cert_path = client_cert_path
        self.client_key_path = client_key_path

        self._lock = threading.Lock()
        self._contentai_cache: Optional[Dict[str, Any]] = None

        self.session = requests.Session()
        self.configure_mtls(ca_cert_path, client_cert_path, client_key_path)

    def configure_mtls(
        self,
        ca_cert_path: Optional[str] = None,
        client_cert_path: Optional[str] = None,
        client_key_path: Optional[str] = None,
    ) -> None:
        """
        Настраивает взаимную аутентификацию TLS (mTLS) на сессии requests.
        Ищет сертификаты в переданных путях, переменных окружения и Windows Certificate Store.
        """
        # 1. Поиск Root CA
        resolved_ca = ca_cert_path or self.ca_cert_path or os.getenv("CA_CERT_PATH")
        if not resolved_ca or not os.path.exists(resolved_ca):
            if find_certificate_file:
                found_ca = find_certificate_file(
                    custom_path=resolved_ca,
                    env_var="CA_CERT_PATH",
                    default_filenames=("ca.crt",)
                )
                if found_ca:
                    resolved_ca = found_ca

        if (not resolved_ca or not os.path.exists(resolved_ca)) and find_root_ca_in_windows_store and export_windows_cert_to_cache:
            win_ca = find_root_ca_in_windows_store()
            if win_ca:
                resolved_ca = export_windows_cert_to_cache(win_ca[0], "windows_remote_root_ca.crt")

        if resolved_ca and os.path.exists(resolved_ca):
            self.ca_cert_path = resolved_ca
            self.session.verify = resolved_ca

        # 2. Поиск клиентского сертификата
        resolved_cert = client_cert_path or self.client_cert_path or os.getenv("ADMIN_CLIENT_CERT_PATH")
        if not resolved_cert or not os.path.exists(resolved_cert):
            if find_certificate_file:
                found_cert = find_certificate_file(
                    custom_path=resolved_cert,
                    env_var="ADMIN_CLIENT_CERT_PATH",
                    default_filenames=("admin_client.crt",)
                )
                if found_cert:
                    resolved_cert = found_cert

        if (not resolved_cert or not os.path.exists(resolved_cert)) and find_client_cert_in_windows_store and export_windows_cert_to_cache:
            win_client = find_client_cert_in_windows_store()
            if win_client:
                resolved_cert = export_windows_cert_to_cache(win_client[0], "windows_remote_admin_client.crt")

        if resolved_cert and os.path.exists(resolved_cert):
            self.client_cert_path = resolved_cert

        # 3. Поиск закрытого ключа клиента
        resolved_key = client_key_path or self.client_key_path or os.getenv("ADMIN_CLIENT_KEY_PATH")
        if not resolved_key or not os.path.exists(resolved_key):
            if find_certificate_file:
                found_key = find_certificate_file(
                    custom_path=resolved_key,
                    env_var="ADMIN_CLIENT_KEY_PATH",
                    default_filenames=("admin_client.key",)
                )
                if found_key:
                    resolved_key = found_key

        if resolved_key and os.path.exists(resolved_key):
            self.client_key_path = resolved_key

        # Монтирование пары mTLS в сессию
        if self.client_cert_path and self.client_key_path:
            self.session.cert = (self.client_cert_path, self.client_key_path)

    def set_server_url(self, server_url: str) -> None:
        """Устанавливает и форматирует целевой URL сервера."""
        url = server_url.strip()
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url
        self.server_url = url.rstrip("/")

    def is_authenticated(self) -> bool:
        """Возвращает флаг наличия активного токена доступа."""
        with self._lock:
            return bool(self.access_token)

    def get_session_info(self) -> Dict[str, Any]:
        """Возвращает текущую информацию о сессии для клиентского UI."""
        with self._lock:
            return {
                "is_authenticated": bool(self.access_token),
                "server_url": self.server_url,
                "username": self.username,
                "role": self.role,
                "has_ca_cert": bool(self.ca_cert_path and os.path.exists(self.ca_cert_path)),
                "has_client_cert": bool(self.client_cert_path and os.path.exists(self.client_cert_path)),
            }

    def login(
        self,
        server_url: str,
        username: str = "admin",
        password: str = "",
        otp_code: str = ""
    ) -> Dict[str, Any]:
        """
        Выполняет вход администратора с валидацией пароля и 2FA TOTP.
        Сохраняет токены сессии в памяти процесса.
        """
        if not server_url:
            raise AuthenticationError("Не указан адрес сервера")
        if not password:
            raise AuthenticationError("Пароль не может быть пустым")

        self.set_server_url(server_url)

        endpoint = f"{self.server_url}/login"
        form_data = {
            "username": username,
            "password": password,
        }
        if otp_code.strip():
            form_data["otp_code"] = otp_code.strip()

        headers = {}
        if otp_code.strip():
            headers["X-OTP-Code"] = otp_code.strip()

        try:
            resp = self.session.post(
                endpoint,
                data=form_data,
                headers=headers,
                timeout=15.0
            )
        except requests.exceptions.SSLError as e:
            raise AuthenticationError(f"Ошибка TLS/mTLS рукопожатия с сервером: {e}")
        except requests.exceptions.RequestException as e:
            raise AuthenticationError(f"Ошибка сетевого соединения с сервером {self.server_url}: {e}")

        if resp.status_code == 200:
            data = resp.json()
            with self._lock:
                self.access_token = data.get("access_token")
                self.refresh_token = data.get("refresh_token")
                self.username = username
                self.role = data.get("role", "admin")
                self._contentai_cache = None
            return {
                "status": "ok",
                "username": self.username,
                "role": self.role,
                "server_url": self.server_url
            }

        # Обработка статусных ошибок
        err_msg = "Ошибка аутентификации"
        try:
            detail = resp.json().get("detail", "")
            if detail == "2FA code required":
                err_msg = "errorEmptyOtp"
            elif detail == "Invalid 2FA TOTP code":
                err_msg = "errorBadOtp"
            elif "already been used" in str(detail):
                err_msg = "errorOtpReused"
            elif detail:
                err_msg = str(detail)
        except Exception:
            err_msg = f"HTTP {resp.status_code}: {resp.text}"

        raise AuthenticationError(err_msg)

    def refresh_session(self) -> bool:
        """
        Выполняет прозрачную ротацию токена доступа (RTR) через удаленный сервер.
        Возвращает True в случае успеха, False при истечении срока сессии.
        """
        with self._lock:
            cur_refresh = self.refresh_token
            cur_url = self.server_url

        if not cur_refresh or not cur_url:
            return False

        endpoint = f"{cur_url}/api/auth/refresh"
        try:
            resp = self.session.post(
                endpoint,
                json={"refresh_token": cur_refresh},
                headers={"Content-Type": "application/json"},
                timeout=10.0
            )
            if resp.status_code == 200:
                data = resp.json()
                with self._lock:
                    self.access_token = data.get("access_token")
                    if data.get("refresh_token"):
                        self.refresh_token = data.get("refresh_token")
                return True
        except Exception:
            pass

        # При сбое ротации очищаем сессию
        self.logout()
        return False

    def logout(self) -> None:
        """Аннулирует активную сессию администратора."""
        with self._lock:
            token = self.access_token
            r_token = self.refresh_token
            url = self.server_url
            self.access_token = None
            self.refresh_token = None
            self.username = None
            self.role = None
            self._contentai_cache = None

        if token and r_token and url:
            try:
                self.session.post(
                    f"{url}/api/auth/logout",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"refresh_token": r_token},
                    timeout=5.0
                )
            except Exception:
                pass

    def pair_2fa(self, username: str, password: str) -> Dict[str, Any]:
        """Запрашивает первичные параметры привязки 2FA (QR-код и Secret)."""
        if not self.server_url:
            raise AuthenticationError("Не указан адрес сервера")
        resp = self.session.post(
            f"{self.server_url}/api/auth/2fa/pair",
            json={"username": username, "password": password},
            timeout=10.0
        )
        if not resp.ok:
            raise AuthenticationError(f"Ошибка генерации 2FA: {resp.text}")
        return resp.json()

    def request(
        self,
        method: str,
        path: str,
        auto_refresh: bool = True,
        **kwargs
    ) -> requests.Response:
        """
        Выполняет авторизованный запрос к удаленному серверу по mTLS
        с прозрачным обновлением токена доступа при ошибке 401.
        """
        if not self.server_url:
            raise RemoteClientError("Целевой сервер не настроен")

        url = f"{self.server_url}/{path.lstrip('/')}"

        headers = kwargs.pop("headers", {}) or {}
        with self._lock:
            if self.access_token and "Authorization" not in headers:
                headers["Authorization"] = f"Bearer {self.access_token}"

        kwargs["headers"] = headers
        if "timeout" not in kwargs:
            kwargs["timeout"] = 30.0

        resp = self.session.request(method, url, **kwargs)

        # Обработка истечения срока токена (401)
        if resp.status_code == 401 and auto_refresh and self.refresh_token:
            refreshed = self.refresh_session()
            if refreshed:
                # Повторяем запрос с новым токеном
                with self._lock:
                    headers["Authorization"] = f"Bearer {self.access_token}"
                kwargs["headers"] = headers
                resp = self.session.request(method, url, **kwargs)
            else:
                raise SessionExpiredError("Срок действия сессии администратора истек")

        return resp

    # --- МЕТОДЫ УПРАВЛЕНИЯ АГЕНТАМИ ---

    def get_agents(self) -> List[Dict[str, Any]]:
        """Возвращает список всех зарегистрированных агентов системы."""
        resp = self.request("GET", "/api/agents")
        resp.raise_for_status()
        return resp.json()

    def suspend_agent(self, agent_name: str) -> Dict[str, Any]:
        """Приостанавливает операции указанного агента."""
        resp = self.request("POST", f"/api/agents/{agent_name}/suspend")
        resp.raise_for_status()
        return resp.json()

    def reactivate_agent(self, agent_name: str) -> Dict[str, Any]:
        """Возобновляет работу приостановленного агента."""
        resp = self.request("POST", f"/api/agents/{agent_name}/reactivate")
        resp.raise_for_status()
        return resp.json()

    def revoke_agent(self, agent_name: str, kid: Optional[str] = None, reason: str = "Compromised") -> Dict[str, Any]:
        """Отзывает криптографический ключ агента (Instant Revocation)."""
        resp = self.request(
            "POST",
            f"/api/agents/{agent_name}/revoke",
            json={"agent_name": agent_name, "kid": kid, "reason": reason}
        )
        resp.raise_for_status()
        return resp.json()

    # --- МЕТОДЫ УПРАВЛЕНИЯ ЗАЯВКАМИ НА ДОКУМЕНТЫ ---

    def get_agent_requests(self) -> List[Dict[str, Any]]:
        """Возвращает список заявок на нормативные документы от агентов."""
        resp = self.request("GET", "/api/agent-requests")
        resp.raise_for_status()
        return resp.json()

    def approve_agent_requests(self, request_ids: List[int]) -> Dict[str, Any]:
        """Одобряет выбранные заявки агентов на документы."""
        resp = self.request(
            "POST",
            "/api/agent-requests/approve",
            json={"request_ids": request_ids}
        )
        resp.raise_for_status()
        return resp.json()

    def reject_agent_requests(self, request_ids: List[int]) -> Dict[str, Any]:
        """Отклоняет выбранные заявки агентов."""
        resp = self.request(
            "POST",
            "/api/agent-requests/reject",
            json={"request_ids": request_ids}
        )
        resp.raise_for_status()
        return resp.json()

    # --- МЕТОДЫ КОНФИГУРАЦИИ RAG ---

    def get_contentai_config(self) -> Dict[str, Any]:
        """
        Безопасно запрашивает учетные данные сервиса Content AI (OCR) с сервера.
        Кэширует конфигурацию в памяти бэкенда, не передавая ее во фронтенд.
        """
        if self._contentai_cache:
            return self._contentai_cache

        resp = self.request("GET", "/api/config/contentai")
        resp.raise_for_status()
        cfg = resp.json()
        with self._lock:
            self._contentai_cache = cfg
        return cfg

    # --- МЕТОДЫ УПРАВЛЕНИЯ КЛЮЧАМИ АГЕНТОВ ---

    def get_agent_keys(self, agent_name: str) -> List[Dict[str, Any]]:
        """Возвращает историю и текущее состояние публичных ключей агента."""
        resp = self.request("GET", f"/api/agents/{agent_name}/keys")
        resp.raise_for_status()
        return resp.json()

    def rotate_agent_key(self, agent_name: str, new_public_key: str, ttl_days: int = 90) -> Dict[str, Any]:
        """Выполняет принудительную ротацию публичного ключа агента."""
        resp = self.request(
            "POST",
            f"/api/agents/{agent_name}/rotate",
            json={"agent_name": agent_name, "new_public_key": new_public_key, "ttl_days": ttl_days}
        )
        resp.raise_for_status()
        return resp.json()

    # --- МЕТОДЫ АУДИТА И КРИПТОГРАФИЧЕСКОГО КОНТРОЛЯ ЦЕЛОСТНОСТИ ---

    def get_audit_logs(
        self,
        limit: int = 50,
        offset: int = 0,
        actor: Optional[str] = None,
        action: Optional[str] = None,
        status: Optional[str] = None
    ) -> Dict[str, Any]:
        """Возвращает страницу записей неизменяемого журнала аудита."""
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if actor:
            params["actor"] = actor
        if action:
            params["action"] = action
        if status:
            params["status"] = status
        resp = self.request("GET", "/api/audit/logs", params=params)
        resp.raise_for_status()
        return resp.json()

    def verify_audit_log(self) -> Dict[str, Any]:
        """Криптографически проверяет целостность всей цепочки хешей журнала аудита (Audit Trail Hash-Chain)."""
        resp = self.request("GET", "/api/audit/verify")
        resp.raise_for_status()
        return resp.json()

    def get_audit_summary(self) -> Dict[str, Any]:
        """Возвращает сводную статистику по журналу аудита и статусу целостности."""
        resp = self.request("GET", "/api/audit/summary")
        resp.raise_for_status()
        return resp.json()

    # --- СТАТУС ВЗАИМНОГО TLS (mTLS) ---

    def get_mtls_status(self) -> Dict[str, Any]:
        """Возвращает информацию о текущем статусе mTLS сессии и настроенных сертификатах."""
        return {
            "server_url": self.server_url,
            "mtls_configured": bool(self.session.cert),
            "ca_configured": bool(self.session.verify and self.session.verify != True),
            "ca_cert_path": self.ca_cert_path,
            "client_cert_path": self.client_cert_path,
            "client_key_path": self.client_key_path,
            "authenticated": self.is_authenticated(),
            "username": self.username,
            "role": self.role,
        }
