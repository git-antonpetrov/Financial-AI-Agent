import os
import asyncio
import uuid
import shutil
import json
from threading import Thread
# pyrefly: ignore [missing-import]
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
# pyrefly: ignore [missing-import]
from fastapi.responses import EventSourceResponse
# pyrefly: ignore [missing-import]
from fastapi.middleware.cors import CORSMiddleware
from pipeline import DocumentPipeline

app = FastAPI(title="Local Admin Client Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "tauri://localhost", "https://tauri.localhost"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# Очереди в памяти для SSE-ответов
job_queues = {}
job_statuses = {}

TEMP_DIR = os.path.join(os.path.dirname(__file__), "temp_uploads")
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
    job_id = str(uuid.uuid4())
    job_queues[job_id] = asyncio.Queue()
    job_statuses[job_id] = "running"
    
    # Ограничение размера в 1 ГБ через чтение частями
    MAX_SIZE = 1024 * 1024 * 1024
    bytes_read = 0
    file_path = os.path.join(TEMP_DIR, f"{job_id}_{file.filename}")
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

@app.get("/api/local/progress/{job_id}")
async def get_progress(job_id: str):
    if job_id not in job_queues:
        raise HTTPException(status_code=404, detail="Job not found")
        
    async def event_generator():
        queue = job_queues[job_id]
        while True:
            # Ожидание события
            event = await queue.get()
            yield {"event": "progress", "data": json.dumps(event)}
            
            if event.get("status") in ["done", "error", "skipped"]:
                break
                
        # Очистка ресурсов
        if job_id in job_queues:
            del job_queues[job_id]
            del job_statuses[job_id]

    return EventSourceResponse(event_generator())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)
