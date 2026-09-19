import uuid
from datetime import datetime, date, timezone
from sqlalchemy import Column, String, Numeric, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from src.simulations.db.digital_ruble.db.database import Base

def generate_uuid():
    return str(uuid.uuid4())

class Wallet(Base):
    __tablename__ = "wallets"
    id = Column(String, primary_key=True, default=generate_uuid)
    client_id = Column(String, unique=True, nullable=False) # 1 кошелек = 1 гражданин
    wallet_number = Column(String, unique=True, nullable=False)
    balance = Column(Numeric(12, 2), default=0.00)
    frozen_balance = Column(Numeric(12, 2), default=0.00)
    status = Column(String, default="active") # active, blocked
    opened_at = Column(DateTime, default=datetime.utcnow)

    sent_transactions = relationship("RubleTransaction", foreign_keys="[RubleTransaction.sender_wallet_id]", back_populates="sender")
    received_transactions = relationship("RubleTransaction", foreign_keys="[RubleTransaction.receiver_wallet_id]", back_populates="receiver")

    created_contracts = relationship("SmartContract", foreign_keys="[SmartContract.creator_wallet_id]", back_populates="creator")
    received_contracts = relationship("SmartContract", foreign_keys="[SmartContract.receiver_wallet_id]", back_populates="receiver")

class RubleTransaction(Base):
    __tablename__ = "ruble_transactions"
    id = Column(String, primary_key=True, default=generate_uuid)
    sender_wallet_id = Column(String, ForeignKey("wallets.id"), nullable=True)
    receiver_wallet_id = Column(String, ForeignKey("wallets.id"), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    status = Column(String, default="completed") # completed, failed
    smart_contract_id = Column(String, ForeignKey("smart_contracts.id"), nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)

    sender = relationship("Wallet", foreign_keys=[sender_wallet_id], back_populates="sent_transactions")
    receiver = relationship("Wallet", foreign_keys=[receiver_wallet_id], back_populates="received_transactions")
    smart_contract = relationship("SmartContract", back_populates="transactions")

class SmartContract(Base):
    __tablename__ = "smart_contracts"
    id = Column(String, primary_key=True, default=generate_uuid)
    creator_wallet_id = Column(String, ForeignKey("wallets.id"), nullable=False)
    receiver_wallet_id = Column(String, ForeignKey("wallets.id"), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    condition_type = Column(String, nullable=False) # "приемка_квартиры", "наступление_даты"
    contract_code = Column(String, nullable=True) # Python DSL код контракта
    condition_status = Column(String, default="pending") # pending, fulfilled, failed
    status = Column(String, default="active") # active, executed, cancelled, failed
    error_message = Column(String, nullable=True) # Полный текст ошибки, если контракт упал
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    executed_at = Column(DateTime, nullable=True)

    creator = relationship("Wallet", foreign_keys=[creator_wallet_id], back_populates="created_contracts")
    receiver = relationship("Wallet", foreign_keys=[receiver_wallet_id], back_populates="received_contracts")
    transactions = relationship("RubleTransaction", back_populates="smart_contract")
