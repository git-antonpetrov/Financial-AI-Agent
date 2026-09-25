import os
import io
import json
from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File, Form, Header
from fastapi.security import OAuth2PasswordRequestForm
# pyrefly: ignore [missing-import]
import redis
from minio import Minio
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import AsyncSession
import litellm
from jose import jwt

from security import verify_password, create_access_token, get_current_admin, verify_agent_jwt, settings
from db.database import engine, Base, get_db
from db import schemas, crud
from core.utils.console_logger import log_info, log_error, log_warning, log_success

VALID_AGENTS = {"main", "bank", "invest", "digital"}
VALID_ACTIONS = {"upsert", "delete"}

# --- НАСТРОЙКА MINIO ---
MINIO_URL = os.getenv("MINIO_URL", "minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")

minio_client = Minio(
    MINIO_URL,
    access_key=MINIO_ACCESS_KEY,
    secret_key=MINIO_SECRET_KEY,
    secure=False
)

# --- НАСТРОЙКА REDIS ---
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")

redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    password=REDIS_PASSWORD,
    decode_responses=True
)

# --- НАСТРОЙКА LLM (через LiteLLM) ---
VERTEX_PROJECT = os.getenv("VERTEX_PROJECT")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "global")
VERTEX_BASE_URL = os.getenv("VERTEX_BASE_URL")

RAG_DATA_MODEL_NAME = os.getenv("RAG_DATA_MODEL_NAME", "vertex_ai/gemini-3.8-flash")
RAG_DATA_REASONING_EFFORT = os.getenv("RAG_DATA_REASONING_EFFORT", "low")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Инициализация бакетов MinIO
    if MINIO_ACCESS_KEY and MINIO_SECRET_KEY:
        for agent in VALID_AGENTS:
            bucket_name = f"knowledge-{agent}"
            if not minio_client.bucket_exists(bucket_name):
                minio_client.make_bucket(bucket_name)
                log_info("MinIO", f"Created bucket: {bucket_name}")
    
    # Инициализация таблиц БД
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        log_info("Database", "Database tables created/verified")
        
    yield

app = FastAPI(title="Financial MAS - Admin Server", version="1.0.0", lifespan=lifespan)

# --- ROUTES: AUTH ---
@app.post("/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    if form_data.username != "admin":
        log_warning("Auth", f"Failed login attempt for user: {form_data.username}")
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    
    if not settings.ADMIN_PASSWORD_HASH:
        log_error("Auth", "Server not configured: ADMIN_PASSWORD_HASH missing")
        raise HTTPException(status_code=500, detail="Server not configured: ADMIN_PASSWORD_HASH missing")

    if not verify_password(form_data.password, settings.ADMIN_PASSWORD_HASH):
        log_warning("Auth", "Failed login attempt (bad password)")
        raise HTTPException(status_code=400, detail="Incorrect username or password")

    access_token = create_access_token(data={"sub": "admin"})
    log_success("Auth", "Successful login")
    return {"access_token": access_token, "token_type": "bearer"}

# --- ROUTES: DOCUMENTS CHECK ---
@app.post("/api/documents/check/md5", response_model=schemas.CheckHashResponse)
async def check_md5(
    req: schemas.CheckHashRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: str = Depends(get_current_admin)
):
    log_info("Check MD5", f"Checking hash for file: {req.filename}")
    status_str = await crud.check_md5(db, req.file_hash, req.filename, req.agent_name)
    return schemas.CheckHashResponse(status=status_str)

@app.post("/api/documents/check/date", response_model=schemas.CheckDateResponse)
async def check_date(
    req: schemas.CheckDateRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: str = Depends(get_current_admin)
):
    log_info("Check Date", f"Checking date for system_name: {req.system_name}")
    status_str = await crud.check_date_version(
        db, req.system_name, req.short_name, req.file_hash, req.filename, req.agent_name
    )
    return schemas.CheckDateResponse(status=status_str)

# --- ROUTES: UPLOAD ---
@app.post("/api/upload/{agent_name}/{action}")
async def upload_document(
    agent_name: str,
    action: str,
    file_hash: str = Form(...),
    system_name: str = Form(None),
    short_name: str = Form(None),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_admin: str = Depends(get_current_admin)
):
    """
    Загружает markdown документ и ставит статус completed.
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
        content = await file.read()
        content_stream = io.BytesIO(content)
        minio_client.put_object(
            bucket_name=bucket_name,
            object_name=object_name,
            data=content_stream,
            length=len(content),
            content_type="text/markdown"
        )
        log_success("MinIO", f"Uploaded {file.filename} to {bucket_name}")
    except Exception as e:
        log_error("MinIO", f"Failed to upload {file.filename}: {str(e)}")
        await crud.create_document(db, file_hash, file.filename, agent_name, "error", system_name, short_name, str(e))
        raise HTTPException(status_code=500, detail=f"MinIO error: {str(e)}")
        
    try:
        message = {
            "agent": agent_name,
            "action": action,
            "file_path": object_name,
            "bucket": bucket_name,
            "file_hash": file_hash,
            "system_name": system_name,
            "short_name": short_name,
            "filename": file.filename
        }
        redis_client.lpush("document_tasks", json.dumps(message))
        log_success("Redis", f"Task queued for {file.filename}")
    except Exception as e:
        log_error("Redis", f"Failed to queue task for {file.filename}: {str(e)}")
        await crud.create_document(db, file_hash, file.filename, agent_name, "error", system_name, short_name, str(e))
        raise HTTPException(status_code=500, detail=f"Redis error: {str(e)}")

    await crud.create_document(db, file_hash, file.filename, agent_name, "processing", system_name, short_name, "Задача в очереди у воркера")

    return {
        "status": "success", 
        "message": f"File {file.filename} uploaded to {bucket_name}/{object_name}",
        "agent": agent_name,
        "action": action
    }

# --- ROUTES: AGENT REQUESTS ---
@app.get("/api/agent_requests", response_model=list[schemas.AgentRequestResponse])
async def read_agent_requests(
    skip: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    current_admin: str = Depends(get_current_admin)
):
    """
    Получение списка заявок от агентов. Поддерживает пагинацию через skip и limit.
    Только для админа.
    """
    requests = await crud.get_agent_requests(db, skip=skip, limit=limit)
    return requests

@app.post("/api/agents/register", response_model=schemas.AgentRegisterResponse)
async def register_agent(
    req: schemas.AgentRegisterRequest,
    x_bootstrap_token: str = Header(...),
    db: AsyncSession = Depends(get_db)
):
    """
    Регистрация публичного ключа агента. Защищено Bootstrap токеном.
    """
    expected_token = settings.get_bootstrap_token(req.agent_name)
    if not expected_token or x_bootstrap_token != expected_token:
        log_warning("Agent Registration", f"Invalid bootstrap token for {req.agent_name}")
        raise HTTPException(status_code=403, detail="Invalid bootstrap token")
    
    try:
        await crud.register_agent(db, req.agent_name, req.public_key)
    except ValueError as e:
        log_warning("Agent Registration", str(e))
        raise HTTPException(status_code=409, detail=str(e))
        
    log_success("Agent Registration", f"Agent {req.agent_name} registered successfully with public key")
    return schemas.AgentRegisterResponse(status="success", message="Public key registered")

@app.post("/api/agent_requests", response_model=schemas.AgentRequestResponse)
async def create_agent_request(
    req: schemas.AgentRequestJWT,
    db: AsyncSession = Depends(get_db)
):
    """
    Создание заявки на документ от агента.
    Эндпоинт проверяет JWT подпись (RS256) используя зарегистрированный публичный ключ агента.
    """
    try:
        # Сначала декодируем без проверки подписи, чтобы узнать, от какого агента токен
        unverified_payload = jwt.get_unverified_claims(req.token)
        agent_name = unverified_payload.get("agent_name")
        document_name = unverified_payload.get("document_name")
        justification = unverified_payload.get("justification")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JWT format")

    if not agent_name or not document_name or not justification:
        raise HTTPException(status_code=400, detail="Missing required claims in JWT")

    # Достаем публичный ключ агента из базы
    agent = await crud.get_agent(db, agent_name)
    if not agent:
        log_warning("Agent Request", f"Agent {agent_name} is not registered")
        raise HTTPException(status_code=404, detail="Agent not registered")

    # Проверяем подпись с помощью публичного ключа агента
    verified_payload = verify_agent_jwt(req.token, agent.public_key)
    if not verified_payload:
        log_warning("Agent Request", f"Invalid RSA signature from agent {agent_name}")
        raise HTTPException(status_code=403, detail="Invalid RSA signature")

    log_info("Agent Request", f"Verified request from {agent_name} for {document_name}")
    return await crud.create_agent_request(db, agent_name, document_name, justification)

# --- ROUTES: LLM PROXY ---
@app.post("/api/llm/analyze", response_model=schemas.LLMAnalyzeResponse)
async def llm_analyze(
    req: schemas.LLMAnalyzeRequest,
    current_admin: str = Depends(get_current_admin)
):
    system_prompt = """
    ТЕБЕ НУЖНО ПРОАНАЛИЗИРОВАТЬ ТЕКСТ ДОКУМЕНТА И ИЗВЛЕЧЬ ДВА ПАРАМЕТРА: system_name И short_name.
    ИНСТРУКЦИЯ СТРОГАЯ, КАК ДЛЯ РЕБЕНКА (ОБЪЯСНЯЮ ПО ШАГАМ, ВЫПОЛНЯЙ В ТОЧНОСТИ):
    
    1. Что такое system_name:
       - Это уникальное системное имя документа. 
       - Оно должно содержать ТОЛЬКО латинские буквы (a-z), цифры (0-9) и символ подчеркивания (_). 
       - НИКАКИХ ПРОБЕЛОВ! НИКАКИХ СЛЕШЕЙ (/) ИЛИ ТОЧЕК (.)! НИКАКИХ РУССКИХ БУКВ!
       - Если видишь русские буквы - делай строгий транслит на английский.
       - Формат всегда такой: [тип_документа]_[номер_документа]_[дата_без_разделителей_ddmmyyyy]
       - Примеры как НАДО делать:
         - Если это "Федеральный Закон №115 от 01.01.2024", то system_name = "fz_115_01012024"
         - Если это "Письмо Банка России №123-И от 15.08.2023", то system_name = "pismo_br_123_i_15082023"
         - Если это "Приказ №1 от 05.02.2025", то system_name = "prikaz_1_05022025"
       
    2. Что такое short_name:
       - Это короткое имя документа (БЕЗ ДАТЫ на конце).
       - То есть ты берешь system_name и просто отрезаешь от него дату.
       - Примеры как НАДО делать:
         - Для "fz_115_01012024" -> short_name = "fz_115"
         - Для "pismo_br_123_i_15082023" -> short_name = "pismo_br_123_i"
    """
    
    prompt = system_prompt + "\n\nТЕКСТ ДЛЯ АНАЛИЗА:\n" + req.text[:15000]
    
    try:
        log_info("LLM", "Starting analyze request")
        response = await litellm.acompletion(
            model=RAG_DATA_MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            response_format=schemas.LLMAnalyzeResponse,
            reasoning_effort=RAG_DATA_REASONING_EFFORT,
            temperature=0.0,
            api_base=VERTEX_BASE_URL,
            vertex_project=VERTEX_PROJECT,
            vertex_location=VERTEX_LOCATION
        )
        
        result_text = response.choices[0].message.content
        clean_json = result_text.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_json)
        return schemas.LLMAnalyzeResponse(**data)
        
    except Exception as e:
        log_error("LLM", f"Error in analyze: {str(e)}")
        raise HTTPException(status_code=500, detail=f"LLM Error: {str(e)}")

@app.post("/api/llm/find_repealed", response_model=schemas.LLMRepealedResponse)
async def llm_find_repealed(
    req: schemas.LLMRepealedRequest,
    current_admin: str = Depends(get_current_admin)
):
    system_prompt = """
    В тексте могут содержаться указания на отмену (утрату силы) старых нормативных актов.
    Твоя задача — найти все документы, которые ОТМЕНЯЮТСЯ (признаются утратившими силу) данным документом.
    Верни их в виде массива short_name (строгий транслит, только буквы, цифры, подчеркивания, без дат — точно так же, как мы формировали short_name ранее).
    
    ПРАВИЛА ИЗВЛЕЧЕНИЯ:
    1. Ищи фразы типа "Признать утратившим силу...", "Отменить..." и т.д.
    2. Извлекай тип документа и номер, превращай в транслит и соединяй через подчеркивание.
    3. Примеры:
       - Текст: "Признать утратившим силу Федеральный закон от 10.07.2002 № 86-ФЗ" -> short_name = "fz_86"
       - Текст: "Отменить Указание Банка России N 1234-У" -> short_name = "ukazanie_br_1234_u"
       - Текст: "Приказ Минфина России №10" -> short_name = "prikaz_minfina_10"
    4. Даты не включай в short_name! 
    5. Если ничего не отменяется, верни пустой массив [].
    """
    
    text_to_analyze = "\n\n---\n\n".join(req.snippets)
    prompt = system_prompt + "\n\nФРАГМЕНТЫ ДЛЯ АНАЛИЗА:\n" + text_to_analyze
    
    try:
        log_info("LLM", "Starting find_repealed request")
        response = await litellm.acompletion(
            model=RAG_DATA_MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            response_format=schemas.LLMRepealedResponse,
            reasoning_effort=RAG_DATA_REASONING_EFFORT,
            temperature=0.0,
            api_base=VERTEX_BASE_URL,
            vertex_project=VERTEX_PROJECT,
            vertex_location=VERTEX_LOCATION
        )
        
        result_text = response.choices[0].message.content
        clean_json = result_text.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_json)
        return schemas.LLMRepealedResponse(**data)
        
    except Exception as e:
        log_error("LLM", f"Error in find_repealed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"LLM Error: {str(e)}")
