import os
import io
from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File
from fastapi.security import OAuth2PasswordRequestForm
from minio import Minio
from security import verify_password, create_access_token, get_current_admin, settings

app = FastAPI(title="Financial MAS - Admin Server", version="1.0.0", lifespan=lifespan)

# Удален CORS, так как API используется только клиентом, а не браузерами

# --- НАСТРОЙКА MINIO ---
# Мы подключимся к MinIO при старте приложения.
MINIO_URL = os.getenv("MINIO_URL", "minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")

minio_client = Minio(
    MINIO_URL,
    access_key=MINIO_ACCESS_KEY,
    secret_key=MINIO_SECRET_KEY,
    secure=False
)

VALID_AGENTS = {"main", "bank", "invest", "digital"}
VALID_ACTIONS = {"upsert", "delete"}

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Убеждаемся, что все необходимые бакеты существуют при запуске."""
    for agent in VALID_AGENTS:
        bucket_name = f"knowledge-{agent}"
        if not minio_client.bucket_exists(bucket_name):
            minio_client.make_bucket(bucket_name)
    yield

# --- ROUTES ---
@app.post("/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    # У нас только один пользователь-администратор
    if form_data.username != "admin":
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    
    if not settings.ADMIN_PASSWORD_HASH:
        raise HTTPException(status_code=500, detail="Server not configured: ADMIN_PASSWORD_HASH missing")

    if not verify_password(form_data.password, settings.ADMIN_PASSWORD_HASH):
        raise HTTPException(status_code=400, detail="Incorrect username or password")

    access_token = create_access_token(data={"sub": "admin"})
    return {"access_token": access_token, "token_type": "bearer"}

@app.post("/api/upload/{agent_name}/{action}")
async def upload_document(
    agent_name: str,
    action: str,
    file: UploadFile = File(...),
    current_admin: str = Depends(get_current_admin)
):
    """
    Загружает markdown документ в корзину (bucket) соответствующего агента.
    """
    if agent_name not in VALID_AGENTS:
        raise HTTPException(status_code=400, detail=f"Invalid agent name. Must be one of {VALID_AGENTS}")
    
    if action not in VALID_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Invalid action. Must be 'upsert' or 'delete'")

    if not file.filename.endswith(".md"):
        raise HTTPException(status_code=400, detail="Only markdown (.md) files are allowed")

    bucket_name = f"knowledge-{agent_name}"
    object_name = f"{action}/{file.filename}"

    try:
        # Чтение содержимого файла
        content = await file.read()
        content_stream = io.BytesIO(content)
        
        # Загрузка в MinIO
        minio_client.put_object(
            bucket_name=bucket_name,
            object_name=object_name,
            data=content_stream,
            length=len(content),
            content_type="text/markdown"
        )
        
        # TODO: Добавить логику публикации сообщения в Redis для Воркера,
        # чтобы он обработал файл из MinIO и загрузил в ChromaDB.

        return {
            "status": "success", 
            "message": f"File {file.filename} uploaded to {bucket_name}/{object_name}",
            "agent": agent_name,
            "action": action
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to upload to storage: {str(e)}")
