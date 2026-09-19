import uuid
from datetime import date, datetime
from sqlalchemy import Column, String, Numeric, Boolean, Date, Time, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from src.simulations.db.bank.db.database import Base

def generate_uuid():
    return str(uuid.uuid4())

class Tariff(Base):
    __tablename__ = "tariffs"
    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, unique=True, nullable=False) # 'Базовый', 'Зарплатный', 'Льготный'
    service_cost = Column(Numeric(12, 2), default=0)
    non_sbp_limit = Column(Numeric(12, 2), default=0)
    sbp_limit = Column(Numeric(12, 2), default=30000000)
    cash_withdrawal_limit = Column(Numeric(12, 2), default=0)
    cash_withdrawal_fee = Column(Numeric(12, 2), default=100)
    transfer_over_limit_fee_percent = Column(Numeric(5, 2), default=2.0)
    deposit_fee = Column(Numeric(12, 2), default=0)

    accounts = relationship("Account", back_populates="tariff")


class Account(Base):
    __tablename__ = "accounts"
    id = Column(String, primary_key=True, default=generate_uuid)
    client_id = Column(String, nullable=False)
    account_number = Column(String, unique=True, nullable=False)
    balance = Column(Numeric(12, 2), default=0.00)
    currency = Column(String, default="RUB")
    tariff_id = Column(String, ForeignKey("tariffs.id"), nullable=False)
    status = Column(String, default="active") # active, closed
    created_at = Column(DateTime, default=datetime.utcnow)

    tariff = relationship("Tariff", back_populates="accounts")
    cards = relationship("Card", back_populates="account")
    transactions = relationship("Transaction", back_populates="account")
    auto_payments = relationship("AutoPayment", back_populates="account")


class Card(Base):
    __tablename__ = "cards"
    id = Column(String, primary_key=True, default=generate_uuid)
    account_id = Column(String, ForeignKey("accounts.id"), nullable=False)
    card_number = Column(String, unique=True, nullable=False)
    status = Column(String, default="active") # active, blocked, frozen
    created_at = Column(DateTime, default=datetime.utcnow)

    account = relationship("Account", back_populates="cards")


class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(String, primary_key=True, default=generate_uuid)
    account_id = Column(String, ForeignKey("accounts.id"), nullable=False)
    category = Column(String, nullable=False) # 'income', 'expense'
    operation_type = Column(String, nullable=False) # 'transfer_sbp', 'transfer_non_sbp', 'purchase', 'top_up', 'cash_deposit', 'salary', etc.
    amount = Column(Numeric(12, 2), nullable=False)
    commission = Column(Numeric(12, 2), default=0.00)
    description = Column(String)
    status = Column(String, default="completed") # 'completed', 'failed', 'pending'
    date = Column(Date, default=date.today)
    
    # We use a callable to set the default time correctly when the object is created
    time = Column(Time, default=lambda: datetime.utcnow().time()) 
    
    account = relationship("Account", back_populates="transactions")


class AutoPayment(Base):
    __tablename__ = "auto_payments"
    id = Column(String, primary_key=True, default=generate_uuid)
    account_id = Column(String, ForeignKey("accounts.id"), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    recipient = Column(String, nullable=False)
    schedule = Column(String, nullable=False) # 'monthly', 'weekly'
    next_payment_date = Column(Date, nullable=False)
    is_active = Column(Boolean, default=True)

    account = relationship("Account", back_populates="auto_payments")


class PaymentReminder(Base):
    __tablename__ = "payment_reminders"
    id = Column(String, primary_key=True, default=generate_uuid)
    client_id = Column(String, nullable=False)
    message = Column(String, nullable=False)
    date = Column(Date, nullable=False)
    is_active = Column(Boolean, default=True)
