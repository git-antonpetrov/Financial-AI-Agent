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
from contextlib import asynccontextmanager
from pipeline import DocumentPipeline

try:
    from src.admin_client.backend.core.crypto import HardwareSigningBridge, ZeroTrustClientSigner
except ImportError:
    try:
        from core.crypto import HardwareSigningBridge, ZeroTrustClientSigner
    except ImportError:
        HardwareSigningBridge = None
        ZeroTrustClientSigner = None

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

    # Разрешает локальную разработку на машине администратора
    if hostname.lower() in {"localhost", "127.0.0.1", "::1"}:
        return

    try:
        addr_info = socket.getaddrinfo(hostname, None)
        for item in addr_info:
            ip_str = item[4][0]
            ip_obj = ipaddress.ip_address(ip_str)
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

@app.post("/api/local/process")
async def process_document(
    agent_name: str = Form(...),
    server_url: str = Form(...),
    admin_token: str = Form(...),
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

    # Валидация server_url с защитой от SSRF
    validate_server_url(server_url)

    # Базовая проверка наличия и структуры JWT admin_token
    if not admin_token or len(admin_token.strip().split(".")) != 3:
        raise HTTPException(status_code=401, detail="Invalid admin_token format")

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
    """Возвращает статус аппаратной безопасности (TPM 2.0 / Windows Hello) и mTLS сертификатов."""
    tpm_available = False
    if HardwareSigningBridge:
        tpm_available = HardwareSigningBridge.is_tpm_available()

    cert_mounted = False
    signer_fp = "none"
    if ZeroTrustClientSigner:
        try:
            signer = ZeroTrustClientSigner()
            signer_fp = signer.get_key_fingerprint()
            cert_mounted = bool(signer.get_certificate_pem())
        except Exception:
            pass

    return {
        "hardware_tpm_available": tpm_available,
        "platform": sys.platform,
        "zero_trust_ready": cert_mounted or tpm_available,
        "client_key_fingerprint": signer_fp
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
