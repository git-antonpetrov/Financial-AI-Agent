import os
import tempfile
import re
import asyncio
import uuid
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.parse
# pyrefly: ignore [missing-import]
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Response, status
# pyrefly: ignore [missing-import]
from fastapi.responses import EventSourceResponse
# pyrefly: ignore [missing-import]
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from pipeline import DocumentPipeline

# Очереди в памяти для SSE-ответов и время их создания
job_queues = {}
job_statuses = {}
job_creation_times = {}

# Пул потоков с ограничением параллельной нагрузки (максимум 3 одновременных файла)
pipeline_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="pipeline_worker")

async def cleanup_stale_jobs():
    """Фоновая задача для очистки устаревших задач (предотвращение утечки памяти)"""
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
async def csrf_origin_guard(request: Request, call_next):
    """
    Защита локального sidecar от несанкционированных Cross-Site запросов из браузера.
    """
    origin = request.headers.get("origin")
    sec_fetch_site = request.headers.get("sec-fetch-site")

    if origin:
        if origin not in ALLOWED_ORIGINS:
            return Response(content="Forbidden: Invalid origin", status_code=status.HTTP_403_FORBIDDEN)
    elif sec_fetch_site == "cross-site":
        return Response(content="Forbidden: Cross-site request rejected", status_code=status.HTTP_403_FORBIDDEN)

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

def run_pipeline(job_id: str, file_path: str, agent_name: str, server_url: str, admin_token: str, contentai_username: str, contentai_password: str, contentai_api_uri: str, loop: asyncio.AbstractEventLoop):
    def progress_callback(filename, status, message):
        event = {
            "status": status,
            "message": message,
            "filename": filename
        }
        if job_id in job_queues:
            loop.call_soon_threadsafe(job_queues[job_id].put_nowait, event)
    
    try:
        pipeline = DocumentPipeline(server_url=server_url, admin_token=admin_token, contentai_username=contentai_username, contentai_password=contentai_password, contentai_api_uri=contentai_api_uri)
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

@app.post("/api/local/process")
async def process_document(
    agent_name: str = Form(...),
    server_url: str = Form(...),
    admin_token: str = Form(...),
    contentai_username: str = Form(""),
    contentai_password: str = Form(""),
    contentai_api_uri: str = Form(""),
    file: UploadFile = File(...)
):
    # Валидация агента, чтобы не тратить ресурсы впустую
    VALID_AGENTS = {"main", "bank", "invest", "digital"}
    if agent_name not in VALID_AGENTS:
        raise HTTPException(status_code=400, detail=f"Invalid agent: {agent_name}")

    # Валидация server_url во избежание SSRF атак на локальные ресурсы
    parsed_server = urllib.parse.urlparse(server_url)
    if parsed_server.scheme not in ["http", "https"] or not parsed_server.netloc:
        raise HTTPException(status_code=400, detail="Invalid server_url format")

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
        loop
    )
    
    return {"job_id": job_id, "filename": file.filename}

@app.get("/api/local/progress/{job_id}", response_class=EventSourceResponse)
async def get_progress(job_id: str):
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
    uvicorn.run(app, host="127.0.0.1", port=8001)
