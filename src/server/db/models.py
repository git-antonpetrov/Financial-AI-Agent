import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime
from src.server.db.database import Base

def generate_uuid():
    return str(uuid.uuid4())

class Client(Base):
    __tablename__ = "clients"
    id = Column(String, primary_key=True, default=generate_uuid)
    full_name = Column(String, nullable=False)
    phone_number = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
