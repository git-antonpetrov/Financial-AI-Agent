"""
Модели SQLAlchemy для хранения документов, заявок агентов и зарегистрированных агентов.
"""

from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .database import Base


class Document(Base):
    """Представляет документ в хранилище, отслеживает его статус обработки и метаданные."""
    __tablename__ = "documents"
    
    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, nullable=False)
    file_hash = Column(String, index=True, nullable=False)
    system_name = Column(String, index=True, nullable=True)
    short_name = Column(String, index=True, nullable=True)
    agent_name = Column(String, index=True, nullable=False)
    status = Column(String, nullable=False)
    message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AgentRequest(Base):
    """Хранит запрос агента на добавление нормативного документа с обоснованием на двух языках."""
    __tablename__ = "agent_requests"
    
    id = Column(Integer, primary_key=True, index=True)
    agent_name = Column(String, index=True, nullable=False)
    document_name_ru = Column(String, nullable=False)
    document_name_en = Column(String, nullable=False)
    justification_ru = Column(Text, nullable=False)
    justification_en = Column(Text, nullable=False)
    status = Column(String, default="pending", nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Agent(Base):
    """Содержит зарегистрированного агента системы и статус его жизненного цикла."""
    __tablename__ = "agents"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    status = Column(String, default="active", nullable=False)  # "active", "suspended", "revoked"
    public_key = Column(String, nullable=True)  # Текущий активный открытый ключ (PEM)
    registered_at = Column(DateTime(timezone=True), server_default=func.now())

    keys = relationship("AgentKey", back_populates="agent", cascade="all, delete-orphan", order_by="desc(AgentKey.created_at)")


class AgentKey(Base):
    """Модель версионирования, ротации и отзыва открытых ключей агентов."""
    __tablename__ = "agent_keys"
    
    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    kid = Column(String, unique=True, index=True, nullable=False)  # Идентификатор ключа (Key ID / Fingerprint)
    public_key = Column(String, nullable=False)  # PEM-формат
    status = Column(String, default="active", nullable=False)  # "active", "superseded", "revoked", "expired"
    is_revoked = Column(Boolean, default=False, nullable=False)
    revocation_reason = Column(String, nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)  # Срок действия ключа (TTL)
    
    agent = relationship("Agent", back_populates="keys")


class AuditLog(Base):
    """
    Неизменяемый криптографически связанный журнал аудита (Tamper-evident Hash-chained Audit Log).
    Каждая запись содержит SHA-256 хэш предыдущей записи (prev_hash), образуя целостную цепочку.
    """
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    actor = Column(String, nullable=False, index=True)
    actor_ip = Column(String, nullable=True)
    action = Column(String, nullable=False, index=True)
    resource = Column(String, nullable=True)
    status = Column(String, nullable=False, index=True)  # SUCCESS, FAILURE, WARNING
    details = Column(Text, nullable=True)
    prev_hash = Column(String, nullable=False)
    record_hash = Column(String, nullable=False, index=True)
