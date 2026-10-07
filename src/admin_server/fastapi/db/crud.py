from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from datetime import datetime, timezone, timedelta
import hashlib
from . import models

def parse_date_from_system_name(system_name: str) -> datetime:
    """Извлекает дату в формате ddmmyyyy из окончания системного имени документа."""
    try:
        if not system_name:
            return datetime.min
        parts = system_name.split("_")
        if len(parts) >= 2:
            date_str = parts[-1]
            if len(date_str) == 8 and date_str.isdigit():
                return datetime.strptime(date_str, "%d%m%Y")
    except Exception:
        pass
    return datetime.min

async def create_document(
    db: AsyncSession,
    file_hash: str,
    filename: str,
    agent_name: str,
    status: str,
    system_name: str = None,
    short_name: str = None,
    message: str = None
) -> models.Document:
    """Создает новую запись о документе в базе данных."""
    doc = models.Document(
        file_hash=file_hash,
        filename=filename,
        agent_name=agent_name,
        status=status,
        system_name=system_name,
        short_name=short_name,
        message=message
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc

async def check_md5(db: AsyncSession, file_hash: str, filename: str, agent_name: str) -> str:
    """
    Проверяет наличие MD5-хэша в базе данных и предотвращает состояние гонки при параллельной загрузке.
    Возвращает 'duplicate', если файл уже загружен или обрабатывается.
    Обновляет устаревшую проверку при превышении таймаута (30 минут).
    Создает запись со статусом 'checking' для нового файла.
    """
    from sqlalchemy import text
    import hashlib

    # Создаем 64-битный хэш из file_hash для блокировки
    lock_id = int(hashlib.md5(file_hash.encode()).hexdigest()[:16], 16) - 2**63
    await db.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id})

    # 1. Проверяем завершенные или обрабатываемые воркером файлы
    query_completed = select(models.Document).where(
        models.Document.file_hash == file_hash,
        models.Document.agent_name == agent_name,
        models.Document.status.in_(['completed', 'processing'])
    )
    result_completed = await db.execute(query_completed)
    completed_doc = result_completed.scalars().first()
    if completed_doc:
        return "duplicate"

    # 2. Проверяем наличие временной записи 'checking'
    query_checking = select(models.Document).where(
        models.Document.file_hash == file_hash,
        models.Document.agent_name == agent_name,
        models.Document.status == 'checking'
    ).order_by(models.Document.created_at.desc())
    result_checking = await db.execute(query_checking)
    checking_doc = result_checking.scalars().first()

    now = datetime.now(timezone.utc)
    STALE_TIMEOUT_SECONDS = 1800  # 30 минут

    if checking_doc:
        doc_time = checking_doc.created_at
        if doc_time:
            if doc_time.tzinfo is None:
                doc_time = doc_time.replace(tzinfo=timezone.utc)
            is_stale = (now - doc_time).total_seconds() > STALE_TIMEOUT_SECONDS
        else:
            is_stale = True

        if is_stale:
            # Предыдущая проверка зависла или оборвалась — перезапускаем ее для нового файла
            checking_doc.created_at = now
            checking_doc.filename = filename
            checking_doc.message = "Файл принят на повторную проверку (предыдущая проверка истекла по таймауту)"
            await db.commit()
            return "ok"
        else:
            # Активная проверка в рамках последних 30 минут
            return "duplicate"

    # Свежий файл — создаём запись 'checking'
    await create_document(db, file_hash, filename, agent_name, "checking", None, None, "Файл принят на проверку")
    return "ok"

async def check_date_version(
    db: AsyncSession, 
    system_name: str, 
    short_name: str, 
    file_hash: str, 
    filename: str, 
    agent_name: str
) -> str:
    """
    Сравнивает дату редакции документа с ранее сохраненными версиями.
    Выполняет поиск по короткому имени документа и по MD5-хэшу.
    """
    from sqlalchemy import or_
    
    query = select(models.Document).where(
        or_(
            models.Document.short_name == short_name,
            models.Document.file_hash == file_hash
        ),
        models.Document.agent_name == agent_name,
        models.Document.status.in_(['completed', 'processing', 'checking'])
    )
    result = await db.execute(query)
    docs = result.scalars().all()
    
    if not docs:
        return "ok"
        
    new_date = parse_date_from_system_name(system_name)
    
    for doc in docs:
        if not doc.system_name:
            continue
        existing_date = parse_date_from_system_name(doc.system_name)
        if new_date <= existing_date:
            message_text = f"Найдена версия с датой {existing_date.strftime('%d.%m.%Y')}, которая новее или равна текущей."
            query_current = select(models.Document).where(
                models.Document.file_hash == file_hash,
                models.Document.agent_name == agent_name,
                models.Document.status == 'checking'
            ).order_by(models.Document.created_at.desc())
            res = await db.execute(query_current)
            checking_doc = res.scalars().first()
            if checking_doc:
                checking_doc.status = "old_version"
                checking_doc.system_name = system_name
                checking_doc.short_name = short_name
                checking_doc.message = message_text
                await db.commit()
            else:
                await create_document(
                    db, file_hash, filename, agent_name, "old_version", 
                    system_name, short_name, message=message_text
                )
            return "old_version"
            
    return "ok"

async def mark_document_error(
    db: AsyncSession,
    file_hash: str,
    filename: str,
    agent_name: str,
    system_name: str = None,
    short_name: str = None,
    error_message: str = None
) -> models.Document:
    """
    Переводит существующую запись в статусе checking/processing в статус error
    во избежание создания дублирующих строк при ошибках.
    """
    query = select(models.Document).where(
        models.Document.file_hash == file_hash,
        models.Document.agent_name == agent_name,
        models.Document.status.in_(['checking', 'processing'])
    ).order_by(models.Document.created_at.desc())
    result = await db.execute(query)
    doc = result.scalars().first()
    if doc:
        doc.status = "error"
        doc.message = error_message
        if system_name:
            doc.system_name = system_name
        if short_name:
            doc.short_name = short_name
        await db.commit()
        return doc
    return await create_document(db, file_hash, filename, agent_name, "error", system_name, short_name, error_message)

async def update_checking_to_processing(
    db: AsyncSession,
    file_hash: str,
    agent_name: str,
    system_name: str,
    short_name: str,
    message: str = "Задача в очереди у воркера"
):
    """
    Обновляет запись со статуса 'checking' на 'processing' после успешной валидации.
    Заполняет системное и короткое имя документа.
    """
    query = select(models.Document).where(
        models.Document.file_hash == file_hash,
        models.Document.agent_name == agent_name,
        models.Document.status == 'checking'
    ).order_by(models.Document.created_at.desc())
    result = await db.execute(query)
    doc = result.scalars().first()
    
    if doc:
        doc.status = "processing"
        doc.system_name = system_name
        doc.short_name = short_name
        doc.message = message
        await db.commit()
        return doc
    
    # Запасной вариант: если 'checking' запись не найдена (не должно быть), создаём новую
    return await create_document(db, file_hash, "", agent_name, "processing", system_name, short_name, message)

async def get_agent_requests(db: AsyncSession, skip: int = 0, limit: int = 50) -> list[models.AgentRequest]:
    """Возвращает список заявок агентов с пагинацией."""
    result = await db.execute(select(models.AgentRequest).order_by(models.AgentRequest.created_at.desc()).offset(skip).limit(limit))
    return result.scalars().all()

async def create_agent_request(
    db: AsyncSession, 
    agent_name: str, 
    document_name_ru: str, 
    document_name_en: str, 
    justification_ru: str,
    justification_en: str
) -> models.AgentRequest:
    """Создает новую заявку от агента на добавление документа."""
    db_req = models.AgentRequest(
        agent_name=agent_name,
        document_name_ru=document_name_ru,
        document_name_en=document_name_en,
        justification_ru=justification_ru,
        justification_en=justification_en,
        status="pending"
    )
    db.add(db_req)
    await db.commit()
    await db.refresh(db_req)
    return db_req

async def update_agent_request_status(db: AsyncSession, request_ids: list[int], status: str):
    """Обновляет статус пакета заявок агентов по их идентификаторам."""
    query = select(models.AgentRequest).where(models.AgentRequest.id.in_(request_ids))
    result = await db.execute(query)
    requests = result.scalars().all()
    for req in requests:
        req.status = status
    await db.commit()

async def get_agent(db: AsyncSession, agent_name: str, include_keys: bool = False) -> models.Agent | None:
    """Возвращает данные зарегистрированного агента по его имени."""
    stmt = select(models.Agent).where(models.Agent.name == agent_name)
    if include_keys:
        stmt = stmt.options(selectinload(models.Agent.keys))
    result = await db.execute(stmt)
    return result.scalars().first()

async def get_active_agent_key(db: AsyncSession, agent_name: str, kid: str | None = None) -> models.AgentKey | None:
    """
    Возвращает активный, не отозванный и не просроченный ключ агента.
    Если указан kid, ищет конкретный ключ; иначе возвращает актуальный активный ключ.
    """
    agent = await get_agent(db, agent_name)
    if not agent or agent.status != "active":
        return None

    query = select(models.AgentKey).where(
        models.AgentKey.agent_id == agent.id,
        models.AgentKey.is_revoked == False,
        models.AgentKey.status == "active"
    )
    if kid:
        query = query.where(models.AgentKey.kid == kid)
    else:
        query = query.order_by(models.AgentKey.created_at.desc())

    result = await db.execute(query)
    key = result.scalars().first()
    if not key:
        return None

    # Проверка истечения срока действия ключа (TTL)
    now = datetime.now(timezone.utc)
    key_exp = key.expires_at
    if key_exp.tzinfo is None:
        key_exp = key_exp.replace(tzinfo=timezone.utc)

    if now > key_exp:
        key.status = "expired"
        if agent.public_key == key.public_key:
            agent.public_key = None
        await db.commit()
        return None

    return key

async def register_agent(db: AsyncSession, agent_name: str, public_key: str, ttl_days: int = 90) -> models.Agent:
    """Регистрирует нового агента или выполняет ротацию ключа для уже существующего."""
    if not public_key.strip().startswith("-----BEGIN"):
        raise ValueError("Public key must be in PEM format (starting with -----BEGIN...)")

    agent = await get_agent(db, agent_name)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=ttl_days)
    kid = f"{agent_name}-{hashlib.sha256(public_key.strip().encode('utf-8')).hexdigest()[:12]}"

    if agent:
        # Плавная ротация ключа: переводим старые активные ключи в статус superseded
        old_keys_query = select(models.AgentKey).where(
            models.AgentKey.agent_id == agent.id,
            models.AgentKey.status == "active",
            models.AgentKey.is_revoked == False
        )
        old_keys_res = await db.execute(old_keys_query)
        for old_k in old_keys_res.scalars().all():
            old_k.status = "superseded"

        agent.public_key = public_key
        agent.status = "active"
        new_key = models.AgentKey(
            agent_id=agent.id,
            kid=kid,
            public_key=public_key,
            status="active",
            is_revoked=False,
            created_at=now,
            expires_at=expires_at
        )
        db.add(new_key)
    else:
        agent = models.Agent(name=agent_name, status="active", public_key=public_key)
        db.add(agent)
        await db.flush()

        new_key = models.AgentKey(
            agent_id=agent.id,
            kid=kid,
            public_key=public_key,
            status="active",
            is_revoked=False,
            created_at=now,
            expires_at=expires_at
        )
        db.add(new_key)

    await db.commit()
    await db.refresh(agent)
    return agent

async def rotate_agent_key(db: AsyncSession, agent_name: str, new_public_key: str, ttl_days: int = 90) -> tuple[models.Agent, models.AgentKey]:
    """Выполняет плановую ротацию ключа агента с архивацией предыдущего."""
    if not new_public_key.strip().startswith("-----BEGIN"):
        raise ValueError("Public key must be in PEM format (starting with -----BEGIN...)")

    agent = await get_agent(db, agent_name)
    if not agent:
        raise ValueError(f"Agent {agent_name} not found")

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=ttl_days)
    kid = f"{agent_name}-{hashlib.sha256(new_public_key.strip().encode('utf-8')).hexdigest()[:12]}"

    old_keys_query = select(models.AgentKey).where(
        models.AgentKey.agent_id == agent.id,
        models.AgentKey.status == "active",
        models.AgentKey.is_revoked == False
    )
    old_keys_res = await db.execute(old_keys_query)
    for old_k in old_keys_res.scalars().all():
        old_k.status = "superseded"

    agent.public_key = new_public_key
    agent.status = "active"
    new_key = models.AgentKey(
        agent_id=agent.id,
        kid=kid,
        public_key=new_public_key,
        status="active",
        is_revoked=False,
        created_at=now,
        expires_at=expires_at
    )
    db.add(new_key)
    await db.commit()
    await db.refresh(agent)
    await db.refresh(new_key)
    return agent, new_key

async def revoke_agent_key(
    db: AsyncSession,
    agent_name: str,
    kid: str | None = None,
    reason: str = "Compromised"
) -> list[models.AgentKey]:
    """Отзывает конкретный или все активные ключи агента."""
    agent = await get_agent(db, agent_name)
    if not agent:
        raise ValueError(f"Agent {agent_name} not found")

    query = select(models.AgentKey).where(
        models.AgentKey.agent_id == agent.id,
        models.AgentKey.is_revoked == False
    )
    if kid:
        query = query.where(models.AgentKey.kid == kid)
    else:
        query = query.where(models.AgentKey.status == "active")

    result = await db.execute(query)
    keys_to_revoke = result.scalars().all()
    if not keys_to_revoke:
        raise ValueError(f"No active unrevoked keys found for agent {agent_name}")

    now = datetime.now(timezone.utc)
    for k in keys_to_revoke:
        k.is_revoked = True
        k.status = "revoked"
        k.revoked_at = now
        k.revocation_reason = reason
        if agent.public_key == k.public_key:
            agent.public_key = None

    await db.commit()
    return keys_to_revoke

async def suspend_agent(db: AsyncSession, agent_name: str) -> models.Agent:
    """Аварийная приостановка активности агента (Emergency Kill Switch)."""
    agent = await get_agent(db, agent_name)
    if not agent:
        raise ValueError(f"Agent {agent_name} not found")
    agent.status = "suspended"
    await db.commit()
    await db.refresh(agent)
    return agent

async def reactivate_agent(db: AsyncSession, agent_name: str) -> models.Agent:
    """Возобновление активности ранее приостановленного агента."""
    agent = await get_agent(db, agent_name)
    if not agent:
        raise ValueError(f"Agent {agent_name} not found")
    agent.status = "active"
    await db.commit()
    await db.refresh(agent)
    return agent

async def list_all_agents(db: AsyncSession) -> list[models.Agent]:
    """Возвращает список всех зарегистрированных агентов с их ключами."""
    query = select(models.Agent).options(selectinload(models.Agent.keys)).order_by(models.Agent.id)
    result = await db.execute(query)
    return result.scalars().all()

async def list_agent_keys(db: AsyncSession, agent_name: str) -> list[models.AgentKey]:
    """Возвращает историю всех ключей агента."""
    agent = await get_agent(db, agent_name)
    if not agent:
        return []
    query = select(models.AgentKey).where(models.AgentKey.agent_id == agent.id).order_by(models.AgentKey.created_at.desc())
    result = await db.execute(query)
    return result.scalars().all()


async def get_system_setting(db: AsyncSession, key: str) -> str | None:
    """Извлекает системную настройку по ключу с прозрачной расшифровкой значения."""
    query = select(models.SystemSetting).where(models.SystemSetting.key == key)
    result = await db.execute(query)
    setting = result.scalars().first()
    return setting.value if setting else None


async def set_system_setting(
    db: AsyncSession,
    key: str,
    value: str,
    description: str | None = None
) -> models.SystemSetting:
    """Сохраняет или обновляет системную настройку с прозрачным шифрованием AES-256-GCM при записи."""
    query = select(models.SystemSetting).where(models.SystemSetting.key == key)
    result = await db.execute(query)
    setting = result.scalars().first()
    if setting:
        setting.value = value
        if description is not None:
            setting.description = description
    else:
        setting = models.SystemSetting(key=key, value=value, description=description)
        db.add(setting)
    await db.commit()
    await db.refresh(setting)
    return setting

