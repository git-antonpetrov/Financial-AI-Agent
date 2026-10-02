from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import datetime, timezone, timedelta
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

async def get_agent(db: AsyncSession, agent_name: str) -> models.Agent:
    """Возвращает данные зарегистрированного агента по его имени."""
    result = await db.execute(select(models.Agent).where(models.Agent.name == agent_name))
    return result.scalars().first()

async def register_agent(db: AsyncSession, agent_name: str, public_key: str) -> models.Agent:
    """Регистрирует нового агента и сохраняет его открытый ключ в системе."""
    if not public_key.strip().startswith("-----BEGIN"):
        raise ValueError("Public key must be in PEM format (starting with -----BEGIN...)")

    agent = await get_agent(db, agent_name)
    if agent:
        raise ValueError("Agent already registered. Key rotation is not supported in MVP.")
    else:
        agent = models.Agent(name=agent_name, public_key=public_key)
        db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent
