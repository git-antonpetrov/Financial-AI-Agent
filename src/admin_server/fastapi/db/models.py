from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.sql import func
from .database import Base

class Document(Base):
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
    __tablename__ = "agent_requests"
    
    id = Column(Integer, primary_key=True, index=True)
    agent_name = Column(String, index=True, nullable=False)
    document_name = Column(String, nullable=False)
    justification = Column(Text, nullable=False)
    status = Column(String, default="Ожидает", nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
