import uuid
from datetime import datetime
from sqlalchemy import Column, String, Numeric, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from src.simulations.db.invest.db.database import Base

def generate_uuid():
    return str(uuid.uuid4())

class InvestmentStrategy(Base):
    __tablename__ = "investment_strategies"
    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, nullable=False)
    expected_yield = Column(Numeric(5, 2), default=0.00) # %
    risk_level = Column(String, nullable=False) # Low, Medium, High
    instruments = Column(String, nullable=False)
    commission_fee = Column(Numeric(5, 2), default=0.00) # % (5 to 25)
    
    broker_accounts = relationship("BrokerAccount", back_populates="strategy")

class SavingsAccount(Base):
    __tablename__ = "savings_accounts"
    id = Column(String, primary_key=True, default=generate_uuid)
    client_id = Column(String, nullable=False)
    account_number = Column(String, unique=True, nullable=False)
    balance = Column(Numeric(12, 2), default=0.00)
    interest_rate = Column(Numeric(5, 2), default=0.00)
    status = Column(String, default="active") # active, closed
    next_payment_date = Column(DateTime, nullable=True)
    next_payment_amount = Column(Numeric(12, 2), default=0.00)
    opened_at = Column(DateTime, default=datetime.utcnow)

class Deposit(Base):
    __tablename__ = "deposits"
    id = Column(String, primary_key=True, default=generate_uuid)
    client_id = Column(String, nullable=False)
    account_number = Column(String, unique=True, nullable=False)
    balance = Column(Numeric(12, 2), default=0.00)
    interest_rate = Column(Numeric(5, 2), default=0.00)
    term_months = Column(Numeric(5, 0), default=12)
    status = Column(String, default="active") # active, closed
    next_payment_date = Column(DateTime, nullable=True)
    next_payment_amount = Column(Numeric(12, 2), default=0.00)
    opened_at = Column(DateTime, default=datetime.utcnow)

class BrokerAccount(Base):
    __tablename__ = "broker_accounts"
    id = Column(String, primary_key=True, default=generate_uuid)
    client_id = Column(String, nullable=False)
    account_number = Column(String, unique=True, nullable=False)
    balance = Column(Numeric(12, 2), default=0.00)
    strategy_id = Column(String, ForeignKey("investment_strategies.id"), nullable=True)
    status = Column(String, default="active") # active, blocked, closed
    opened_at = Column(DateTime, default=datetime.utcnow)
    
    strategy = relationship("InvestmentStrategy", back_populates="broker_accounts")
