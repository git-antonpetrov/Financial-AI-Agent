"""
Модели SQLAlchemy для хранения документов, заявок агентов и зарегистрированных агентов.
"""

from sqlalchemy import Column, Integer, String, Text, DateTime
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
    """Содержит зарегистрированного агента системы и его открытый ключ для верификации подписей."""
    __tablename__ = "agents"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    public_key = Column(String, nullable=False)
    registered_at = Column(DateTime(timezone=True), server_default=func.now())
