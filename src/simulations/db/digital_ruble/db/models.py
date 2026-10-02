import uuid
from datetime import datetime, date, timezone, timedelta
from sqlalchemy import Column, String, Numeric, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from src.simulations.db.digital_ruble.db.database import Base

def generate_uuid():
    return str(uuid.uuid4())

def get_moscow_now():
    return (datetime.now(timezone.utc) + timedelta(hours=3)).replace(tzinfo=None)

class Wallet(Base):
    __tablename__ = "wallets"
    id = Column(String, primary_key=True, default=generate_uuid)
    client_id = Column(String, unique=True, nullable=False, index=True) # 1 кошелек = 1 гражданин
    wallet_number = Column(String, unique=True, nullable=False)
    balance = Column(Numeric(12, 2), default=0.00)
    frozen_balance = Column(Numeric(12, 2), default=0.00)
    status = Column(String, default="active", index=True)  # Возможные значения: active, blocked
    opened_at = Column(DateTime, default=get_moscow_now)

    sent_transactions = relationship("RubleTransaction", foreign_keys="[RubleTransaction.sender_wallet_id]", back_populates="sender")
    received_transactions = relationship("RubleTransaction", foreign_keys="[RubleTransaction.receiver_wallet_id]", back_populates="receiver")

    created_contracts = relationship("SmartContract", foreign_keys="[SmartContract.creator_wallet_id]", back_populates="creator")
    received_contracts = relationship("SmartContract", foreign_keys="[SmartContract.receiver_wallet_id]", back_populates="receiver")

class RubleTransaction(Base):
    __tablename__ = "ruble_transactions"
    id = Column(String, primary_key=True, default=generate_uuid)
    sender_wallet_id = Column(String, ForeignKey("wallets.id"), nullable=True, index=True)
    receiver_wallet_id = Column(String, ForeignKey("wallets.id"), nullable=False, index=True)
    amount = Column(Numeric(12, 2), nullable=False)
    status = Column(String, default="completed", index=True)  # Возможные значения: completed, failed
    smart_contract_id = Column(String, ForeignKey("smart_contracts.id"), nullable=True, index=True)
    signature = Column(String, nullable=True)
    timestamp = Column(DateTime, default=get_moscow_now, index=True)

    sender = relationship("Wallet", foreign_keys=[sender_wallet_id], back_populates="sent_transactions")
    receiver = relationship("Wallet", foreign_keys=[receiver_wallet_id], back_populates="received_transactions")
    smart_contract = relationship("SmartContract", back_populates="transactions")

class SmartContract(Base):
    __tablename__ = "smart_contracts"
    id = Column(String, primary_key=True, default=generate_uuid)
    creator_wallet_id = Column(String, ForeignKey("wallets.id"), nullable=False, index=True)
    receiver_wallet_id = Column(String, ForeignKey("wallets.id"), nullable=False, index=True)
    amount = Column(Numeric(12, 2), nullable=False)
    condition_type = Column(String, nullable=False)  # "приемка_квартиры", "наступление_даты"
    contract_code = Column(String, nullable=True)  # Python DSL код контракта
    condition_status = Column(String, default="pending", index=True)  # Возможные значения: pending, fulfilled, failed
    status = Column(String, default="active", index=True)  # Возможные значения: active, executed, cancelled, failed
    error_message = Column(String, nullable=True)  # Полный текст ошибки при сбое контракта
    created_at = Column(DateTime, default=get_moscow_now)
    executed_at = Column(DateTime, nullable=True)

    creator = relationship("Wallet", foreign_keys=[creator_wallet_id], back_populates="created_contracts")
    receiver = relationship("Wallet", foreign_keys=[receiver_wallet_id], back_populates="received_contracts")
    transactions = relationship("RubleTransaction", back_populates="smart_contract")
