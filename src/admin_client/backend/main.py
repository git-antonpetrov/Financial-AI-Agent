import os
import sys
import tempfile
import re
import asyncio
import uuid
import time
import socket
import ipaddress
from concurrent.futures import ThreadPoolExecutor
import urllib.parse
import secrets
import requests
# pyrefly: ignore [missing-import]
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Response, status
# pyrefly: ignore [missing-import]
try:
    from sse_starlette.sse import EventSourceResponse
except ImportError:
    try:
        from fastapi.responses import EventSourceResponse
    except ImportError:
        from starlette.responses import StreamingResponse as EventSourceResponse
# pyrefly: ignore [missing-import]
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
from contextlib import asynccontextmanager
from pipeline import DocumentPipeline

try:
    from src.admin_client.backend.core.crypto import (
        HardwareSigningBridge,
        ZeroTrustClientSigner,
        find_root_ca_in_windows_store,
        find_client_cert_in_windows_store,
        get_windows_cert_store_help_message,
        WindowsCertificateStoreError,
        CertificateMissingError,
        CertificateValidationError,
    )
except ImportError:
    try:
        from core.crypto import (
            HardwareSigningBridge,
            ZeroTrustClientSigner,
            find_root_ca_in_windows_store,
            find_client_cert_in_windows_store,
            get_windows_cert_store_help_message,
            WindowsCertificateStoreError,
            CertificateMissingError,
            CertificateValidationError,
        )
    except ImportError:
        HardwareSigningBridge = None
        ZeroTrustClientSigner = None
        find_root_ca_in_windows_store = None
        find_client_cert_in_windows_store = None
        get_windows_cert_store_help_message = lambda x: "Сертификаты Zero-Trust не найдены"
        WindowsCertificateStoreError = RuntimeError
        CertificateMissingError = RuntimeError
        CertificateValidationError = RuntimeError

try:
    from src.admin_client.backend.core.remote_client import (
        RemoteAdminClient,
        AuthenticationError,
        SessionExpiredError,
        RemoteClientError,
    )
except ImportError:
    try:
        from core.remote_client import (
            RemoteAdminClient,
            AuthenticationError,
            SessionExpiredError,
            RemoteClientError,
        )
    except ImportError:
        RemoteAdminClient = None
        AuthenticationError = Exception
        SessionExpiredError = Exception
        RemoteClientError = Exception

# Инициализация синглтона удаленного доверенного клиента (BFF)
remote_client = RemoteAdminClient() if RemoteAdminClient else None

def get_remote_client() -> RemoteAdminClient:
    """Возвращает инициализированный доверенный клиент или вызывает ошибку сервера."""
    if not remote_client:
        raise HTTPException(status_code=500, detail="RemoteAdminClient is not available")
    return remote_client


class LoginRequest(BaseModel):
    server_url: str
    username: str = "admin"
    password: str
    otp_code: str = ""


class Pair2FARequest(BaseModel):
    server_url: Optional[str] = None
    username: str = "admin"
    password: str


class AgentRevokeRequest(BaseModel):
    kid: Optional[str] = None
    reason: str = "Compromised"


class AgentRotateKeyRequest(BaseModel):
    new_public_key: str
    ttl_days: Optional[int] = 90


class AgentBatchRequest(BaseModel):
    request_ids: List[int]


def handle_remote_error(e: Exception):
    """Преобразует исключения удаленного взаимодействия в корректные HTTP-ответы."""
    if isinstance(e, SessionExpiredError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    if isinstance(e, requests.HTTPError) and e.response is not None:
        status_code = e.response.status_code
        detail_msg = e.response.text
        try:
            detail_msg = e.response.json().get("detail", detail_msg)
        except Exception:
            pass
        raise HTTPException(status_code=status_code, detail=detail_msg)
    if isinstance(e, AuthenticationError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    if isinstance(e, RemoteClientError):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

# Очереди в памяти для SSE-ответов и время их создания
job_queues = {}
job_statuses = {}
job_creation_times = {}

# Пул потоков с ограничением параллельной нагрузки (максимум 3 одновременных файла)
pipeline_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="pipeline_worker")

# Секрет рукопожатия IPC, переданный родительским процессом Tauri
SIDECAR_IPC_SECRET = os.getenv("SIDECAR_IPC_SECRET", "").strip()

# Защита от автономного запуска скомпилированного бинарника в обход родительского процесса
if getattr(sys, "frozen", False) and not SIDECAR_IPC_SECRET:
    sys.stderr.write("FATAL: Standalone execution of frozen sidecar is prohibited. Must be spawned by Tauri with SIDECAR_IPC_SECRET.\n")
    sys.exit(1)

async def cleanup_stale_jobs():
    """Выполняет периодическую фоновую очистку устаревших задач для предотвращения утечки памяти."""
    while True:
        try:
            await asyncio.sleep(300)  # Проверяем каждые 5 минут
            current_time = time.time()
            stale_jobs = [
                job_id for job_id, created_at in list(job_creation_times.items()) 
                if current_time - created_at > 3600
            ]  # Удаляем старше 1 часа
            for job_id in stale_jobs:
                job_queues.pop(job_id, None)
                job_statuses.pop(job_id, None)
                job_creation_times.pop(job_id, None)
        except asyncio.CancelledError:
            break
        except Exception:
            pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Управляет жизненным циклом приложения FastAPI, запуская и корректно останавливая фоновые задачи."""
    # Запускаем фоновую очистку при старте приложения
    cleanup_task = asyncio.create_task(cleanup_stale_jobs())
    yield
    cleanup_task.cancel()
    pipeline_executor.shutdown(wait=False)

app = FastAPI(title="Local Admin Client Backend", lifespan=lifespan)

ALLOWED_ORIGINS = {
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "tauri://localhost",
    "https://tauri.localhost",
    "http://tauri.localhost",
    "asset://localhost",
}

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(ALLOWED_ORIGINS),
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

@app.middleware("http")
async def ipc_security_guard(request: Request, call_next):
    """Выполняет комплексную защиту локального sidecar:
    1. Защита от межсайтовых атак (Origin / CSRF).
    2. Проверка криптографического токена рукопожатия IPC (X-Local-Secret / ?secret=).
    """
    if request.method == "OPTIONS":
        return await call_next(request)

    origin = request.headers.get("origin")
    sec_fetch_site = request.headers.get("sec-fetch-site")

    if origin:
        if origin not in ALLOWED_ORIGINS:
            return Response(content="Forbidden: Invalid origin", status_code=status.HTTP_403_FORBIDDEN)
    elif sec_fetch_site == "cross-site":
        return Response(content="Forbidden: Cross-site request rejected", status_code=status.HTTP_403_FORBIDDEN)

    # Проверка IPC токена рукопожатия от доверенного родительского процесса
    if SIDECAR_IPC_SECRET:
        token = request.headers.get("x-local-secret") or request.query_params.get("secret")
        if not token or not secrets.compare_digest(token.strip(), SIDECAR_IPC_SECRET):
            return Response(content="Forbidden: Invalid or missing IPC secret", status_code=status.HTTP_403_FORBIDDEN)

    return await call_next(request)


TEMP_DIR = os.path.join(tempfile.gettempdir(), "financial_ai_admin_uploads")
os.makedirs(TEMP_DIR, exist_ok=True)
# Безопасная очистка старых временных файлов (старше 24 часов) для исключения race condition с активными задачами
try:
    now_ts = time.time()
    STALE_FILE_MAX_AGE = 24 * 3600  # 24 часа
    for filename in os.listdir(TEMP_DIR):
        file_path = os.path.join(TEMP_DIR, filename)
        if os.path.isfile(file_path):
            try:
                file_age = now_ts - os.path.getmtime(file_path)
                if file_age > STALE_FILE_MAX_AGE:
                    os.remove(file_path)
            except Exception:
                pass
except Exception:
    pass

def run_pipeline(
    job_id: str,
    file_path: str,
    agent_name: str,
    server_url: str,
    admin_token: str,
    contentai_username: str,
    contentai_password: str,
    contentai_api_uri: str,
    loop: asyncio.AbstractEventLoop,
    ca_cert_path: str = "",
    client_cert_path: str = "",
    client_key_path: str = "",
    use_zero_trust: bool = True
):
    """Запускает процесс обработки файла в отдельном рабочем потоке и передает события прогресса в очередь."""
    def progress_callback(filename, status, message):
        event = {
            "status": status,
            "message": message,
            "filename": filename
        }
        if job_id in job_queues:
            loop.call_soon_threadsafe(job_queues[job_id].put_nowait, event)
    
    try:
        pipeline = DocumentPipeline(
            server_url=server_url,
            admin_token=admin_token,
            contentai_username=contentai_username,
            contentai_password=contentai_password,
            contentai_api_uri=contentai_api_uri,
            ca_cert_path=ca_cert_path or None,
            client_cert_path=client_cert_path or None,
            client_key_path=client_key_path or None,
            use_zero_trust=use_zero_trust
        )
        pipeline.set_progress_callback(progress_callback)
        
        result = pipeline.process_file(file_path, agent_name)
        
        # Отправка финального события об успехе
        if job_id in job_queues:
            loop.call_soon_threadsafe(job_queues[job_id].put_nowait, {
                "status": "done",
                "message": "Pipeline finished.",
                "result": result
            })
    except Exception as e:
        if job_id in job_queues:
            loop.call_soon_threadsafe(job_queues[job_id].put_nowait, {
                "status": "error",
                "message": f"Pipeline failed: {str(e)}"
            })
    finally:
        job_statuses[job_id] = "finished"
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass

def validate_server_url(server_url: str) -> None:
    """
    Проверяет URL сервера на корректность формата и блокирует потенциальные SSRF-атаки.
    
    Разрешает стандартные схемы http/https, публичные домены и IP-адреса, а также
    локальные адреса разработки (localhost, 127.0.0.1, ::1). Отклоняет обращения к
    приватным подсетям, link-local, loopback (не localhost) и мультикаст адресам.
    """
    parsed_server = urllib.parse.urlparse(server_url)
    if parsed_server.scheme not in ["http", "https"] or not parsed_server.netloc:
        raise HTTPException(status_code=400, detail="Invalid server_url format")

    hostname = parsed_server.hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="Invalid server hostname")

    admin_local_domain = os.getenv("ADMIN_LOCAL_DOMAIN", "admin.fin-ai-agent.local").strip().lower()
    allowed_hostnames = {"localhost", "127.0.0.1", "::1"}
    if admin_local_domain:
        allowed_hostnames.add(admin_local_domain)

    # Разрешает локальную разработку и доверенный административный локальный домен
    if hostname.lower() in allowed_hostnames:
        return

    # Разрешенные корпоративные подсети (например, для внутренних VDS/on-premise серверов)
    enterprise_subnets_env = os.getenv("ENTERPRISE_SUBNETS", "")
    enterprise_networks = []
    if enterprise_subnets_env:
        for cidr in enterprise_subnets_env.split(","):
            cidr = cidr.strip()
            if cidr:
                try:
                    enterprise_networks.append(ipaddress.ip_network(cidr, strict=False))
                except ValueError:
                    pass

    try:
        addr_info = socket.getaddrinfo(hostname, None)
        for item in addr_info:
            ip_str = item[4][0]
            ip_obj = ipaddress.ip_address(ip_str)

            # Проверяем вхождение в доверенные корпоративные подсети
            if any(ip_obj in net for net in enterprise_networks):
                continue

            if (
                ip_obj.is_private
                or ip_obj.is_link_local
                or ip_obj.is_loopback
                or ip_obj.is_reserved
                or ip_obj.is_multicast
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"SSRF protection: access to private or local network ({hostname}) is forbidden",
                )
    except socket.gaierror:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid server_url: unable to resolve hostname {hostname}",
        )

@app.post("/api/local/auth/login")
async def local_auth_login(req: LoginRequest):
    """Выполняет аутентификацию администратора на удаленном сервере по mTLS."""
    client = get_remote_client()
    try:
        validate_server_url(req.server_url)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        return client.login(
            server_url=req.server_url,
            username=req.username,
            password=req.password,
            otp_code=req.otp_code,
        )
    except Exception as e:
        handle_remote_error(e)


@app.get("/api/local/auth/status")
async def local_auth_status():
    """Возвращает информацию о текущей активной сессии администратора."""
    client = get_remote_client()
    return client.get_session_info()


@app.post("/api/local/auth/logout")
async def local_auth_logout():
    """Аннулирует текущую сессию администратора."""
    client = get_remote_client()
    client.logout()
    return {"status": "ok"}


@app.post("/api/local/auth/2fa/pair")
async def local_auth_2fa_pair(req: Pair2FARequest):
    """Инициирует генерацию секрета и QR-кода двухфакторной аутентификации (2FA)."""
    client = get_remote_client()
    if req.server_url:
        validate_server_url(req.server_url)
        client.set_server_url(req.server_url)
    try:
        return client.pair_2fa(username=req.username, password=req.password)
    except Exception as e:
        handle_remote_error(e)


@app.get("/api/local/agents")
async def local_get_agents():
    """Возвращает актуальный список всех агентов системы."""
    client = get_remote_client()
    if not client.is_authenticated():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin session not authenticated")
    try:
        return client.get_agents()
    except Exception as e:
        handle_remote_error(e)


@app.post("/api/local/agents/{agent_name}/suspend")
async def local_suspend_agent(agent_name: str):
    """Приостанавливает операции указанного агента."""
    client = get_remote_client()
    if not client.is_authenticated():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin session not authenticated")
    try:
        return client.suspend_agent(agent_name)
    except Exception as e:
        handle_remote_error(e)


@app.post("/api/local/agents/{agent_name}/reactivate")
async def local_reactivate_agent(agent_name: str):
    """Возобновляет функционирование приостановленного агента."""
    client = get_remote_client()
    if not client.is_authenticated():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin session not authenticated")
    try:
        return client.reactivate_agent(agent_name)
    except Exception as e:
        handle_remote_error(e)


@app.post("/api/local/agents/{agent_name}/revoke")
async def local_revoke_agent(agent_name: str, req: AgentRevokeRequest = AgentRevokeRequest()):
    """Выполняет немедленный отзыв криптографического ключа агента (Instant Revocation)."""
    client = get_remote_client()
    if not client.is_authenticated():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin session not authenticated")
    try:
        return client.revoke_agent(agent_name, kid=req.kid, reason=req.reason)
    except Exception as e:
        handle_remote_error(e)


@app.get("/api/local/agents/{agent_name}/keys")
async def local_get_agent_keys(agent_name: str):
    """Возвращает историю открытых ключей агента."""
    client = get_remote_client()
    if not client.is_authenticated():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin session not authenticated")
    try:
        return client.get_agent_keys(agent_name)
    except Exception as e:
        handle_remote_error(e)


@app.post("/api/local/agents/{agent_name}/rotate")
async def local_rotate_agent_key(agent_name: str, req: AgentRotateKeyRequest):
    """Выполняет ротацию открытого ключа агента."""
    client = get_remote_client()
    if not client.is_authenticated():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin session not authenticated")
    try:
        return client.rotate_agent_key(agent_name, req.new_public_key, req.ttl_days or 90)
    except Exception as e:
        handle_remote_error(e)


@app.get("/api/local/audit/logs")
async def local_get_audit_logs(
    limit: int = 50,
    offset: int = 0,
    actor: Optional[str] = None,
    action: Optional[str] = None,
    status: Optional[str] = None
):
    """Возвращает список событий неизменяемого журнала аудита."""
    client = get_remote_client()
    if not client.is_authenticated():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin session not authenticated")
    try:
        return client.get_audit_logs(limit=limit, offset=offset, actor=actor, action=action, status=status)
    except Exception as e:
        handle_remote_error(e)


@app.get("/api/local/audit/verify")
async def local_verify_audit_log():
    """Проверяет криптографическую целостность хеш-цепочки журнала аудита."""
    client = get_remote_client()
    if not client.is_authenticated():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin session not authenticated")
    try:
        return client.verify_audit_log()
    except Exception as e:
        handle_remote_error(e)


@app.get("/api/local/audit/summary")
async def local_get_audit_summary():
    """Возвращает сводную статистику по журналу аудита."""
    client = get_remote_client()
    if not client.is_authenticated():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin session not authenticated")
    try:
        return client.get_audit_summary()
    except Exception as e:
        handle_remote_error(e)


@app.get("/api/local/auth/mtls/status")
async def local_get_mtls_status():
    """Возвращает статус взаимной аутентификации TLS (mTLS) и сертификатов."""
    client = get_remote_client()
    return client.get_mtls_status()


@app.get("/api/local/agent-requests")
async def local_get_agent_requests():
    """Возвращает список запросов на регламентные документы от агентов."""
    client = get_remote_client()
    if not client.is_authenticated():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin session not authenticated")
    try:
        return client.get_agent_requests()
    except Exception as e:
        handle_remote_error(e)


@app.post("/api/local/agent-requests/approve")
async def local_approve_agent_requests(req: AgentBatchRequest):
    """Одобряет выбранные заявки агентов на доступ к документам."""
    client = get_remote_client()
    if not client.is_authenticated():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin session not authenticated")
    try:
        return client.approve_agent_requests(req.request_ids)
    except Exception as e:
        handle_remote_error(e)


@app.post("/api/local/agent-requests/reject")
async def local_reject_agent_requests(req: AgentBatchRequest):
    """Отклоняет выбранные заявки агентов."""
    client = get_remote_client()
    if not client.is_authenticated():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin session not authenticated")
    try:
        return client.reject_agent_requests(req.request_ids)
    except Exception as e:
        handle_remote_error(e)


@app.post("/api/local/process")
async def process_document(
    agent_name: str = Form(...),
    server_url: Optional[str] = Form(None),
    admin_token: Optional[str] = Form(None),
    contentai_username: str = Form(""),
    contentai_password: str = Form(""),
    contentai_api_uri: str = Form(""),
    ca_cert_path: str = Form(""),
    client_cert_path: str = Form(""),
    client_key_path: str = Form(""),
    use_zero_trust: bool = Form(True),
    file: UploadFile = File(...)
):
    """Принимает файл и параметры запуска, валидирует входные данные и ставит задачу обработки в очередь."""
    # Валидация агента, чтобы не тратить ресурсы впустую
    VALID_AGENTS = {"main", "bank", "invest", "digital"}
    if agent_name not in VALID_AGENTS:
        raise HTTPException(status_code=400, detail=f"Invalid agent: {agent_name}")

    # Подтягиваем параметры из активной сессии remote_client, если они не переданы напрямую
    if not server_url and remote_client:
        server_url = remote_client.server_url
    if not admin_token and remote_client:
        admin_token = remote_client.access_token

    if not server_url:
        raise HTTPException(status_code=400, detail="server_url is required or session must be authenticated")

    # Валидация server_url с защитой от SSRF
    validate_server_url(server_url)

    # Базовая проверка наличия и структуры JWT admin_token
    if not admin_token or len(admin_token.strip().split(".")) != 3:
        raise HTTPException(status_code=401, detail="Invalid admin_token format or not authenticated")

    # Автоматическое получение учетных данных Content AI (OCR) через бэкенд, исключая утечку во фронтенд
    if remote_client and not (contentai_username and contentai_password and contentai_api_uri):
        try:
            cfg = remote_client.get_contentai_config()
            contentai_username = contentai_username or cfg.get("username", "")
            contentai_password = contentai_password or cfg.get("password", "")
            contentai_api_uri = contentai_api_uri or cfg.get("api_uri", "")
        except Exception:
            pass

    if remote_client:
        ca_cert_path = ca_cert_path or remote_client.ca_cert_path or ""
        client_cert_path = client_cert_path or remote_client.client_cert_path or ""
        client_key_path = client_key_path or remote_client.client_key_path or ""

    # Проверка Zero-Trust сертификатов перед постановкой задачи
    if use_zero_trust:
        has_ca = (
            bool(ca_cert_path and os.path.exists(ca_cert_path))
            or bool(os.getenv("CA_CERT_PATH") and os.path.exists(os.getenv("CA_CERT_PATH", "")))
            or os.path.exists("./certs/ca.crt")
            or os.path.exists("certs/ca.crt")
            or (bool(find_root_ca_in_windows_store and find_root_ca_in_windows_store()))
        )
        has_cert = (
            bool(client_cert_path and os.path.exists(client_cert_path))
            or bool(os.getenv("ADMIN_CLIENT_CERT_PATH") and os.path.exists(os.getenv("ADMIN_CLIENT_CERT_PATH", "")))
            or os.path.exists("./certs/admin_client.crt")
            or os.path.exists("certs/admin_client.crt")
            or (bool(find_client_cert_in_windows_store and find_client_cert_in_windows_store()))
        )
        if not has_ca or not has_cert:
            missing = "all" if (not has_ca and not has_cert) else ("root_ca" if not has_ca else "client_cert")
            error_instruction = get_windows_cert_store_help_message(missing)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": "WINDOWS_CERT_STORE_CERTIFICATE_MISSING",
                    "missing_component": missing,
                    "message": error_instruction
                }
            )

    job_id = str(uuid.uuid4())
    job_queues[job_id] = asyncio.Queue()
    job_statuses[job_id] = "running"
    job_creation_times[job_id] = time.time()
    
    # Ограничение размера в 100 МБ через чтение частями
    MAX_SIZE = 100 * 1024 * 1024
    bytes_read = 0
    raw_name = os.path.basename(file.filename or "uploaded_file")
    safe_filename = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', raw_name).strip('._') or "file"
    file_path = os.path.join(TEMP_DIR, f"{job_id}_{safe_filename}")
    try:
        with open(file_path, "wb") as buffer:
            while True:
                chunk = await file.read(1024 * 1024 * 5)  # Части по 5 МБ
                if not chunk:
                    break
                bytes_read += len(chunk)
                if bytes_read > MAX_SIZE:
                    raise HTTPException(status_code=413, detail="File too large. Maximum size is 100MB.")
                buffer.write(chunk)
    except Exception as e:
        if os.path.exists(file_path):
            os.remove(file_path)
        raise e
        
    # Запуск задачи через ограниченный пул потоков
    loop = asyncio.get_running_loop()
    pipeline_executor.submit(
        run_pipeline,
        job_id,
        file_path,
        agent_name,
        server_url,
        admin_token,
        contentai_username,
        contentai_password,
        contentai_api_uri,
        loop,
        ca_cert_path,
        client_cert_path,
        client_key_path,
        use_zero_trust
    )
    
    return {"job_id": job_id, "filename": file.filename}

@app.get("/api/local/security/status")
async def get_security_status():
    """Возвращает статус аппаратной безопасности (TPM 2.0 / Windows Hello) и системного хранилища сертификатов Windows."""
    tpm_available = False
    if HardwareSigningBridge:
        tpm_available = HardwareSigningBridge.is_tpm_available()

    win_client_found = False
    win_root_found = False
    if find_client_cert_in_windows_store:
        try:
            win_client_found = bool(find_client_cert_in_windows_store())
        except Exception:
            pass
    if find_root_ca_in_windows_store:
        try:
            win_root_found = bool(find_root_ca_in_windows_store())
        except Exception:
            pass

    cert_mounted = False
    signer_fp = "none"
    signer_error = None
    if ZeroTrustClientSigner:
        try:
            signer = ZeroTrustClientSigner()
            signer_fp = signer.get_key_fingerprint()
            cert_mounted = bool(signer.get_certificate_pem())
        except Exception as e:
            signer_error = str(e)

    # Определение статуса хранилища сертификатов Windows
    if win_client_found and win_root_found:
        store_status = "ready"
        store_instruction = None
    elif not win_root_found and not win_client_found:
        store_status = "missing_both"
        store_instruction = get_windows_cert_store_help_message("all")
    elif not win_root_found:
        store_status = "missing_root_ca"
        store_instruction = get_windows_cert_store_help_message("root_ca")
    else:
        store_status = "missing_client_cert"
        store_instruction = get_windows_cert_store_help_message("client_cert")

    return {
        "hardware_tpm_available": tpm_available,
        "platform": sys.platform,
        "zero_trust_ready": cert_mounted or (tpm_available and (win_root_found or bool(os.getenv("CA_CERT_PATH")))),
        "client_key_fingerprint": signer_fp,
        "windows_cert_store": {
            "client_cert_found": win_client_found,
            "root_ca_found": win_root_found,
            "status": store_status,
            "instruction": store_instruction
        },
        "signer_error": signer_error
    }

@app.get("/api/local/progress/{job_id}", response_class=EventSourceResponse)
async def get_progress(job_id: str):
    """Предоставляет SSE-поток событий о ходе обработки документа для клиентского интерфейса."""
    if job_id not in job_queues:
        raise HTTPException(status_code=404, detail="Job not found")
        
    queue = job_queues[job_id]
    
    is_completed = False
    try:
        while True:
            # Ожидание события
            event = await queue.get()
            yield event
            
            if event.get("status") in ["done", "error", "skipped"]:
                is_completed = True
                break
    finally:
        # Очищаем ресурсы только если получен терминальный статус или пайплайн уже завершен
        if is_completed or job_statuses.get(job_id) == "finished":
            job_queues.pop(job_id, None)
            job_statuses.pop(job_id, None)
            job_creation_times.pop(job_id, None)

if __name__ == "__main__":
    import uvicorn
    import argparse

    parser = argparse.ArgumentParser(description="Financial MAS Admin Client Sidecar")
    parser.add_argument("--port", type=int, default=int(os.getenv("SIDECAR_PORT", "8005")), help="Port to listen on")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host interface to bind to")
    args, _ = parser.parse_known_args()

    uvicorn.run(app, host=args.host, port=args.port)
