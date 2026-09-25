from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import datetime
from . import models

def parse_date_from_system_name(system_name: str) -> datetime:
    """Извлекает дату ddmmyyyy из конца системного имени, например fz_115_01012024"""
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
    Если хеш найден и статус completed, возвращаем 'duplicate'. Иначе 'ok'.
    Создаем запись в истории, если это дубликат.
    """
    query = select(models.Document).where(
        models.Document.file_hash == file_hash,
        models.Document.agent_name == agent_name,
        models.Document.status == 'completed'
    )
    result = await db.execute(query)
    existing = result.scalars().first()
    
    if existing:
        await create_document(db, file_hash, filename, agent_name, "duplicate", existing.system_name, existing.short_name, "Файл является полным дубликатом по хешу")
        return "duplicate"
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
    Сравнивает дату с уже загруженными версиями этого short_name.
    """
    query = select(models.Document).where(
        models.Document.short_name == short_name,
        models.Document.agent_name == agent_name,
        models.Document.status == 'completed'
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
            await create_document(
                db, file_hash, filename, agent_name, "old_version", 
                system_name, short_name, message=f"Найдена версия с датой {existing_date.strftime('%d.%m.%Y')}, которая новее или равна текущей."
            )
            return "old_version"
            
    return "ok"

async def get_agent_requests(db: AsyncSession, skip: int = 0, limit: int = 50) -> list[models.AgentRequest]:
    result = await db.execute(select(models.AgentRequest).order_by(models.AgentRequest.created_at.desc()).offset(skip).limit(limit))
    return result.scalars().all()

async def create_agent_request(db: AsyncSession, agent_name: str, document_name: str, justification: str) -> models.AgentRequest:
    db_req = models.AgentRequest(
        agent_name=agent_name,
        document_name=document_name,
        justification=justification,
        status="Ожидает"
    )
    db.add(db_req)
    await db.commit()
    await db.refresh(db_req)
    return db_req

async def get_agent(db: AsyncSession, agent_name: str) -> models.Agent:
    result = await db.execute(select(models.Agent).where(models.Agent.name == agent_name))
    return result.scalars().first()

async def register_agent(db: AsyncSession, agent_name: str, public_key: str) -> models.Agent:
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
