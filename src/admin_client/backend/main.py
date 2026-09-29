import os
import asyncio
import uuid
import time
from threading import Thread
# pyrefly: ignore [missing-import]
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
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

async def cleanup_stale_jobs():
    """Фоновая задача для очистки устаревших задач (предотвращение утечки памяти)"""
    while True:
        await asyncio.sleep(300)  # Проверяем каждые 5 минут
        current_time = time.time()
        stale_jobs = [job_id for job_id, created_at in job_creation_times.items() 
                      if current_time - created_at > 3600]  # Удаляем старше 1 часа
        for job_id in stale_jobs:
            job_queues.pop(job_id, None)
            job_statuses.pop(job_id, None)
            job_creation_times.pop(job_id, None)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Запускаем фоновую очистку при старте приложения
    cleanup_task = asyncio.create_task(cleanup_stale_jobs())
    yield
    cleanup_task.cancel()

app = FastAPI(title="Local Admin Client Backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "tauri://localhost", "https://tauri.localhost"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


TEMP_DIR = os.path.join(os.path.dirname(__file__), "temp_uploads")
# Очищаем директорию временных файлов при запуске, чтобы не засорять диск
if os.path.exists(TEMP_DIR):
    for filename in os.listdir(TEMP_DIR):
        file_path = os.path.join(TEMP_DIR, filename)
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
        except Exception:
            pass
os.makedirs(TEMP_DIR, exist_ok=True)

def run_pipeline(job_id: str, file_path: str, agent_name: str, server_url: str, admin_token: str, loop: asyncio.AbstractEventLoop):
    def progress_callback(filename, status, message):
        event = {
            "status": status,
            "message": message,
            "filename": filename
        }
        if job_id in job_queues:
            loop.call_soon_threadsafe(job_queues[job_id].put_nowait, event)
    
    try:
        pipeline = DocumentPipeline(server_url=server_url, admin_token=admin_token)
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
    file: UploadFile = File(...)
):
    # Валидация агента, чтобы не тратить ресурсы впустую
    VALID_AGENTS = {"main", "bank", "invest", "digital"}
    if agent_name not in VALID_AGENTS:
        raise HTTPException(status_code=400, detail=f"Invalid agent: {agent_name}")

    job_id = str(uuid.uuid4())
    job_queues[job_id] = asyncio.Queue()
    job_statuses[job_id] = "running"
    job_creation_times[job_id] = time.time()
    
    # Ограничение размера в 1 ГБ через чтение частями
    MAX_SIZE = 1024 * 1024 * 1024
    bytes_read = 0
    safe_filename = os.path.basename(file.filename)
    file_path = os.path.join(TEMP_DIR, f"{job_id}_{safe_filename}")
    try:
        with open(file_path, "wb") as buffer:
            while True:
                chunk = await file.read(1024 * 1024 * 10)  # Части по 10 МБ
                if not chunk:
                    break
                bytes_read += len(chunk)
                if bytes_read > MAX_SIZE:
                    raise HTTPException(status_code=413, detail="File too large. Maximum size is 1GB.")
                buffer.write(chunk)
    except Exception as e:
        if os.path.exists(file_path):
            os.remove(file_path)
        raise e
        
    # Запуск фонового потока
    loop = asyncio.get_running_loop()
    thread = Thread(target=run_pipeline, args=(job_id, file_path, agent_name, server_url, admin_token, loop))
    thread.start()
    
    return {"job_id": job_id, "filename": file.filename}

@app.get("/api/local/progress/{job_id}", response_class=EventSourceResponse)
async def get_progress(job_id: str):
    if job_id not in job_queues:
        raise HTTPException(status_code=404, detail="Job not found")
        
    queue = job_queues[job_id]
    
    try:
        while True:
            # Ожидание события
            event = await queue.get()
            yield event
            
            if event.get("status") in ["done", "error", "skipped"]:
                break
    finally:
        # Очистка ресурсов при закрытии соединения
        job_queues.pop(job_id, None)
        job_statuses.pop(job_id, None)
        job_creation_times.pop(job_id, None)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)
