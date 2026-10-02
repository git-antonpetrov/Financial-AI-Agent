import os
import re
import io
import json
import secrets
import tempfile
import anyio
import ipaddress
from datetime import datetime, timezone
from fastapi import FastAPI, Depends, HTTPException, Request, status, UploadFile, File, Form, Header, Query
# pyrefly: ignore [missing-import]
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
# pyrefly: ignore [missing-import]
import redis
from minio import Minio
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import AsyncSession
import litellm
import jwt

from security import verify_password, create_access_token, get_current_admin, verify_agent_jwt, settings, oauth2_scheme
from db.database import engine, Base, get_db, validate_database_env
from db import schemas, crud
from core.utils.console_logger import log_info, log_error, log_warning, log_success
from urllib.parse import quote_plus

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
    decode_responses=True,
    socket_timeout=2.0,
    socket_connect_timeout=2.0
)

# --- НАСТРОЙКА LLM (через LiteLLM) ---
VERTEX_PROJECT = os.getenv("VERTEX_PROJECT")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "global")
VERTEX_BASE_URL = os.getenv("VERTEX_BASE_URL")

def get_vertex_api_base(model_name: str) -> str | None:
    if not VERTEX_BASE_URL:
        return None
    clean_model = model_name.replace("vertex_ai/", "")
    return f"{VERTEX_BASE_URL.rstrip('/')}/v1/projects/{VERTEX_PROJECT}/locations/{VERTEX_LOCATION}/publishers/google/models/{clean_model}"
RAG_DATA_MODEL_NAME = os.getenv("RAG_DATA_MODEL_NAME", "vertex_ai/gemini-3.8-flash")
RAG_DATA_REASONING_EFFORT = os.getenv("RAG_DATA_REASONING_EFFORT", "low")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Валидация обязательных настроек перед стартом сервера
    settings.validate_production_env()
    validate_database_env()

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

# Ограничиваем CORS только необходимыми origin, так как работаем из WebView
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8001", 
        "http://localhost:5173", 
        "tauri://localhost", 
        "https://tauri.localhost",
        "http://tauri.localhost",
        "asset://localhost",
        "https://admin.fin-ai-agent.ru" # Разрешаем CORS для production домена
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "x-bootstrap-token"],
)

@app.get("/health")
async def health_check():
    return {"status": "ok"}


def get_client_ip(request: Request) -> str:
    """
    Извлекает реальный IP-адрес клиента с защитой от спуфинга X-Forwarded-For.
    За обратным прокси Caddy доверенный IP клиента добавляется в конец списка X-Forwarded-For.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        ips = [ip.strip() for ip in forwarded.split(",") if ip.strip()]
        if ips:
            candidate = ips[-1]
            try:
                ipaddress.ip_address(candidate)
                return candidate
            except ValueError:
                pass
    if request.client and request.client.host:
        try:
            ipaddress.ip_address(request.client.host)
            return request.client.host
        except ValueError:
            pass
    return "127.0.0.1"

def check_rate_limit(key: str, max_requests: int = 15, window_seconds: int = 60) -> bool:
    """
    Проверяет лимит запросов через Redis.
    Возвращает True если лимит не превышен, False если превышен.
    """
    if not redis_client:
        return True
    try:
        current = redis_client.incr(key)
        if current == 1:
            redis_client.expire(key, window_seconds)
        return current <= max_requests
    except Exception as e:
        log_warning("RateLimit", f"Failed to check rate limit in Redis: {e}")
        return True

# --- МАРШРУТЫ: АВТОРИЗАЦИЯ ---
@app.post("/login", response_model=schemas.TokenResponse)
async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    """Выполняет аутентификацию администратора с выдачей JWT-токена доступа."""
    client_ip = get_client_ip(request)
    origin = request.headers.get("origin")
    log_info("Auth", f"Запрос на логин, IP: {client_ip}, Origin: {origin}")

    # Защита от DoS/bcrypt CPU exhaustion: не более 15 обращений к /login в минуту с одного IP
    if not check_rate_limit(f"ratelimit:login:{client_ip}", max_requests=15, window_seconds=60):
        log_warning("Auth", f"Rate limit exceeded for login from IP: {client_ip}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Please try again later."
        )

    # Защита от подбора пароля: не более 5 неудачных попыток в течение 5 минут
    failed_key = f"ratelimit:login_failed:{client_ip}"
    try:
        failed_count = int(redis_client.get(failed_key) or 0)
        if failed_count >= 5:
            log_warning("Auth", f"Account lockout: IP {client_ip} has 5+ failed login attempts")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed login attempts. IP temporarily blocked for 5 minutes."
            )
    except HTTPException:
        raise
    except Exception as e:
        log_warning("Auth", f"Failed to check failed attempts in Redis: {e}")

    if not secrets.compare_digest(form_data.username, "admin"):
        try:
            curr = redis_client.incr(failed_key)
            if curr == 1:
                redis_client.expire(failed_key, 300)
        except Exception:
            pass
        log_warning("Auth", f"Failed login attempt for user: {form_data.username} from IP: {client_ip}")
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    
    if not settings.ADMIN_PASSWORD_HASH:
        log_error("Auth", "Server not configured: ADMIN_PASSWORD_HASH missing")
        raise HTTPException(status_code=500, detail="Server not configured: ADMIN_PASSWORD_HASH missing")

    if not verify_password(form_data.password, settings.ADMIN_PASSWORD_HASH):
        try:
            curr = redis_client.incr(failed_key)
            if curr == 1:
                redis_client.expire(failed_key, 300)
        except Exception:
            pass
        log_warning("Auth", f"Failed login attempt (bad password) from IP: {client_ip}")
        raise HTTPException(status_code=400, detail="Incorrect username or password")

    # Сбрасываем счетчик неудачных попыток при успешной авторизации
    try:
        redis_client.delete(failed_key)
    except Exception:
        pass

    access_token = create_access_token(data={"sub": "admin"})
    log_success("Auth", f"Successful login from IP: {client_ip}")
    return {"access_token": access_token, "token_type": "bearer"}

@app.post("/api/auth/logout")
async def logout(
    token: str = Depends(oauth2_scheme),
    current_admin: str = Depends(get_current_admin)
):
    """
    Отзыв токена. Добавляет jti и токен в Redis Blacklist с оставшимся TTL.
    """
    from security import redis_blacklist, settings
    if redis_blacklist:
        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
            jti = payload.get("jti")
            exp = payload.get("exp")
            now_ts = int(datetime.now(timezone.utc).timestamp())
            ttl = max(1, exp - now_ts) if exp else settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60

            if jti:
                redis_blacklist.setex(f"blacklist:{jti}", ttl, "revoked")
            redis_blacklist.setex(f"blacklist:{token}", ttl, "revoked")
        except Exception as e:
            log_error("Auth", f"Failed to blacklist token in Redis: {str(e)}")
            
    log_success("Auth", "Successful logout, token revoked in Redis")
    return {"status": "ok", "message": "Token revoked successfully"}

@app.get("/api/config/contentai", response_model=schemas.ContentAiConfigResponse)
async def get_contentai_config(current_admin: str = Depends(get_current_admin)):
    """Возвращает учетные данные сервиса Content AI для локального sidecar клиента."""
    return schemas.ContentAiConfigResponse(
        username=os.getenv("CONTENTAI_USERNAME", ""),
        password=os.getenv("CONTENTAI_PASSWORD", ""),
        api_uri=os.getenv("CONTENT_AI_API_URI", "")
    )

# --- МАРШРУТЫ: ПРОВЕРКА ДОКУМЕНТОВ ---
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

# --- МАРШРУТЫ: ЗАГРУЗКА ДОКУМЕНТОВ ---
@app.post("/api/upload/{agent_name}/{action}", response_model=schemas.UploadResponse)
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
    Загружает markdown документ в MinIO и помещает задачу векторизации в очередь Redis.
    """
    if agent_name not in VALID_AGENTS:
        raise HTTPException(status_code=400, detail=f"Invalid agent name. Must be one of {VALID_AGENTS}")
    
    if action not in VALID_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Invalid action. Must be 'upsert' or 'delete'")

    if not file.filename or not file.filename.endswith(".md"):
        raise HTTPException(status_code=400, detail="Only markdown (.md) files are allowed")

    # Ограничение размера до 50 МБ для Markdown файлов (защита от OOM DoS)
    MAX_SIZE = 50 * 1024 * 1024
    
    # SpooledTemporaryFile: буферизует до 5 МБ в памяти, свыше — автоматически сбрасывает во временный файл на диске
    content_stream = tempfile.SpooledTemporaryFile(max_size=5 * 1024 * 1024, mode="w+b")
    bytes_read = 0
    try:
        while True:
            chunk = await file.read(1024 * 1024) # читаем по 1 МБ
            if not chunk:
                break
            bytes_read += len(chunk)
            if bytes_read > MAX_SIZE:
                raise HTTPException(status_code=413, detail="File too large. Maximum size for markdown is 50MB.")
            content_stream.write(chunk)
            
        content_stream.seek(0)

        # Очистка имени файла для предотвращения path traversal (кроссплатформенная, включая Windows \ на Linux)
        raw_name = (file.filename or "").replace("\\", "/").split("/")[-1]
        safe_filename = re.sub(r'[^a-zA-Z0-9_.-]', '_', raw_name)
        if not safe_filename or not safe_filename.strip("_.") or safe_filename.startswith("."):
            safe_filename = f"upload_{file_hash[:8]}.md"
        if not safe_filename.endswith(".md"):
            safe_filename += ".md"

        bucket_name = f"knowledge-{agent_name}"
        object_name = f"{action}/{safe_filename}"

        try:
            await anyio.to_thread.run_sync(
                lambda: minio_client.put_object(
                    bucket_name=bucket_name,
                    object_name=object_name,
                    data=content_stream,
                    length=bytes_read,
                    content_type="text/markdown"
                )
            )
            log_success("MinIO", f"Uploaded {file.filename} to {bucket_name}")
        except Exception as e:
            log_error("MinIO", f"Failed to upload {file.filename}: {str(e)}")
            await crud.mark_document_error(db, file_hash, file.filename, agent_name, system_name, short_name, str(e))
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
            await anyio.to_thread.run_sync(redis_client.lpush, "document_tasks", json.dumps(message))
            log_success("Redis", f"Task queued for {file.filename}")
        except Exception as e:
            log_error("Redis", f"Failed to queue task for {file.filename}: {str(e)}")
            await crud.mark_document_error(db, file_hash, file.filename, agent_name, system_name, short_name, str(e))
            raise HTTPException(status_code=500, detail=f"Redis error: {str(e)}")

        if action == "delete":
            await crud.create_document(
                db,
                file_hash=file_hash,
                filename=safe_filename,
                agent_name=agent_name,
                status="processing",
                system_name=system_name,
                short_name=short_name,
                message="Служебная задача удаления устаревших актов в очереди"
            )
        else:
            await crud.update_checking_to_processing(db, file_hash, agent_name, system_name, short_name, "Задача в очереди у воркера")

        return {
            "status": "success", 
            "message": f"File {file.filename} uploaded to {bucket_name}/{object_name}",
            "agent": agent_name,
            "action": action
        }
    finally:
        content_stream.close()

# --- МАРШРУТЫ: ЗАЯВКИ АГЕНТОВ ---
@app.get("/api/agent-requests", response_model=list[schemas.AgentRequestResponse])
async def read_agent_requests(
    skip: int = Query(default=0, ge=0, description="Количество пропускаемых записей"),
    limit: int = Query(default=50, ge=1, le=100, description="Количество возвращаемых записей (максимум 100)"),
    db: AsyncSession = Depends(get_db),
    current_admin: str = Depends(get_current_admin)
):
    """
    Возвращает список заявок от агентов с поддержкой пагинации через skip и limit.
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
    if not expected_token or not secrets.compare_digest(x_bootstrap_token, expected_token):
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
        # Декодируем claims без проверки подписи только для определения имени агента
        unverified_payload = jwt.decode(req.token, options={"verify_signature": False})
        agent_name = unverified_payload.get("agent_name")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JWT format")

    if not agent_name:
        raise HTTPException(status_code=400, detail="Missing 'agent_name' claim in JWT")

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

    # Извлекаем все поля заявки строго из верифицированного payload
    verified_agent_name = verified_payload.get("agent_name")
    if verified_agent_name != agent_name:
        log_warning("Agent Request", f"Agent name mismatch between unverified ({agent_name}) and verified ({verified_agent_name}) claims")
        raise HTTPException(status_code=403, detail="Agent name mismatch")

    document_name_ru = verified_payload.get("document_name_ru")
    document_name_en = verified_payload.get("document_name_en") or ""
    justification_ru = verified_payload.get("justification_ru")
    justification_en = verified_payload.get("justification_en") or ""

    if not document_name_ru or not justification_ru:
        raise HTTPException(status_code=400, detail="Missing required claims (document_name_ru, justification_ru) in verified JWT")

    log_info("Agent Request", f"Verified request from {verified_agent_name} for {document_name_ru}")
    return await crud.create_agent_request(
        db, verified_agent_name, document_name_ru, document_name_en, justification_ru, justification_en
    )

# --- МАРШРУТЫ: ПРОКСИ LLM ---
def extract_json_from_llm(result_text: str | None) -> dict | list:
    """
    Извлекает и парсит JSON из ответа LLM, даже если он содержит
    markdown-блоки (```json ... ```), теги рассуждений (<think>...) или поясняющий текст.
    """
    if not result_text or not result_text.strip():
        raise ValueError("Пустой ответ от LLM")
    
    # 1. Удаляем теги <think>...</think>, если они присутствуют
    cleaned = re.sub(r'<think>.*?</think>', '', result_text, flags=re.DOTALL).strip()
    
    # 2. Проверяем наличие markdown fenced block: ```json ... ``` или ``` ... ```
    fence_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', cleaned, re.IGNORECASE)
    if fence_match:
        content_candidate = fence_match.group(1).strip()
        try:
            return json.loads(content_candidate)
        except json.JSONDecodeError:
            pass

    # 3. Ищем самый внешний JSON-объект {...} или массив [...]
    start_brace = cleaned.find('{')
    start_bracket = cleaned.find('[')
    
    if start_brace != -1 and (start_bracket == -1 or start_brace < start_bracket):
        end_brace = cleaned.rfind('}')
        if end_brace != -1 and end_brace > start_brace:
            candidate = cleaned[start_brace:end_brace + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
    elif start_bracket != -1:
        end_bracket = cleaned.rfind(']')
        if end_bracket != -1 and end_bracket > start_bracket:
            candidate = cleaned[start_bracket:end_bracket + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

    # 4. В качестве fallback пробуем распарсить очищенную строку напрямую
    return json.loads(cleaned)

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
            api_base=get_vertex_api_base(RAG_DATA_MODEL_NAME),
            vertex_project=VERTEX_PROJECT,
            vertex_location=VERTEX_LOCATION
        )
        
        result_text = response.choices[0].message.content
        parsed = extract_json_from_llm(result_text)
        if not isinstance(parsed, dict):
            raise ValueError(f"Ожидался JSON-объект, получен {type(parsed)}")
        return schemas.LLMAnalyzeResponse(**parsed)
        
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
            api_base=get_vertex_api_base(RAG_DATA_MODEL_NAME),
            vertex_project=VERTEX_PROJECT,
            vertex_location=VERTEX_LOCATION
        )
        
        result_text = response.choices[0].message.content
        parsed = extract_json_from_llm(result_text)
        if isinstance(parsed, list):
            data = {"short_names": parsed}
        elif isinstance(parsed, dict):
            if "short_names" not in parsed and len(parsed) == 1:
                val = next(iter(parsed.values()))
                if isinstance(val, list):
                    data = {"short_names": val}
                else:
                    data = parsed
            else:
                data = parsed
        else:
            raise ValueError(f"Неожиданный формат данных от LLM: {type(parsed)}")
        return schemas.LLMRepealedResponse(**data)
        
    except Exception as e:
        log_error("LLM", f"Error in find_repealed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"LLM Error: {str(e)}")

# --- МАРШРУТЫ: УПРАВЛЕНИЕ ЗАЯВКАМИ АГЕНТОВ ---

@app.post("/api/agent-requests/approve", response_model=schemas.BatchActionResponse)
async def approve_agent_requests(
    req: schemas.AgentRequestBatchAction,
    db: AsyncSession = Depends(get_db),
    current_admin: str = Depends(get_current_admin)
):
    """Одобряет пакет заявок агентов по переданным идентификаторам."""
    await crud.update_agent_request_status(db, req.request_ids, "approved")
    return schemas.BatchActionResponse(status="ok")

@app.post("/api/agent-requests/reject", response_model=schemas.BatchActionResponse)
async def reject_agent_requests(
    req: schemas.AgentRequestBatchAction,
    db: AsyncSession = Depends(get_db),
    current_admin: str = Depends(get_current_admin)
):
    """Отклоняет пакет заявок агентов по переданным идентификаторам."""
    await crud.update_agent_request_status(db, req.request_ids, "rejected")
    return schemas.BatchActionResponse(status="ok")

@app.post("/api/agent-requests/bootstrap", response_model=schemas.AgentRequestResponse)
async def create_agent_request_endpoint(
    req: schemas.AgentRequestCreate,
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(...)
):
    """Создает заявку от имени агента с валидацией его bootstrap-токена."""
    expected_token = settings.get_bootstrap_token(req.agent_name)
    expected_auth = f"Bearer {expected_token}" if expected_token else ""
    if not expected_token or not secrets.compare_digest(authorization, expected_auth):
        log_warning("API", f"Invalid bootstrap token for agent {req.agent_name}")
        raise HTTPException(status_code=401, detail="Invalid token")
        
    return await crud.create_agent_request(
        db, 
        req.agent_name, 
        req.document_name_ru, 
        req.document_name_en, 
        req.justification_ru, 
        req.justification_en
    )
