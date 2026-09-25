from typing import Optional
import random
# pyrefly: ignore [missing-import]
from fastapi import APIRouter, HTTPException
from sqlalchemy.future import select
from pydantic import BaseModel

from src.simulations.db.invest.db.client import get_async_session_maker as get_invest_session
from src.simulations.db.bank.db.client import get_async_session_maker as get_bank_session
from src.simulations.db.invest.db.models import InvestmentStrategy, SavingsAccount, Deposit, BrokerAccount
from src.simulations.db.bank.db.models import Account, Transaction
from src.simulations.core.utils.console_logger import log_info, log_error, log_success
from src.simulations.api.core.utils import to_dict, to_dict_list

router = APIRouter()

def invest_db():
    """Фабрика для получения сессии инвестиционной БД."""
    return get_invest_session()()

def bank_db():
    """Фабрика для получения сессии банковской БД."""
    return get_bank_session()()

# --- Схемы данных (Pydantic) ---

class OpenProductRequest(BaseModel):
    """Схема запроса для открытия инвестиционного продукта (вклад, копилка, брокерский счет)."""
    client_id: str
    from_bank_account_id: str
    initial_amount: float
    term_months: Optional[int] = None # Обязательно для вкладов

class StrategySubscription(BaseModel):
    """Схема для управления подпиской на инвестиционную стратегию."""
    strategy_id: Optional[str] = None

class CloseProductRequest(BaseModel):
    """Схема для закрытия продукта."""
    to_bank_account_id: str

# --- Эндпоинты ---

@router.get("/strategies")
async def get_strategies():
    """
    Получить список всех доступных инвестиционных стратегий.
    """
    async with invest_db() as db:
        result = await db.execute(select(InvestmentStrategy))
        return to_dict_list(result.scalars().all())

@router.get("/products/terms")
async def get_terms():
    """
    Получить текущие условия по продуктам (ставки, сроки).
    """
    # Мок-данные стандартных условий
    return {
        "key_rate": 15.0,
        "deposit_rates": {"6_months": 14.5, "12_months": 15.2},
        "savings_account_rate": 10.0
    }

@router.get("/portfolio/{client_id}")
async def get_portfolio(client_id: str):
    """
    Получить сводный инвестиционный портфель клиента (копилки, вклады, брокерские счета).
    """
    async with invest_db() as db:
        savings = (await db.execute(select(SavingsAccount).where(SavingsAccount.client_id == client_id))).scalars().all()
        deposits = (await db.execute(select(Deposit).where(Deposit.client_id == client_id))).scalars().all()
        brokers = (await db.execute(select(BrokerAccount).where(BrokerAccount.client_id == client_id))).scalars().all()
        
        log_info("InvestAPI", f"Собран портфель для клиента {client_id}")
        return {
            "savings_accounts": to_dict_list(savings),
            "deposits": to_dict_list(deposits),
            "broker_accounts": to_dict_list(brokers)
        }

@router.post("/deposits/open")
async def open_deposit(req: OpenProductRequest):
    """
    Открыть новый вклад. 
    Кросс-доменная операция: списывает средства со счета в банке и создает актив в инвестициях.
    """
    if not req.term_months:
        log_error("InvestAPI", "Попытка открыть вклад без указания срока (term_months)")
        raise HTTPException(400, "Для вклада необходимо указать срок (term_months)")
    
    # 1. Списываем средства со счета в банке
    async with bank_db() as bdb:
        acc = await bdb.scalar(select(Account).where(Account.id == req.from_bank_account_id))
        if not acc: raise HTTPException(404, "Банковский счет не найден")
        
        if float(acc.balance) < req.initial_amount:
            log_error("InvestAPI", f"Недостаточно средств на банковском счете {req.from_bank_account_id} для открытия вклада")
            raise HTTPException(400, "Недостаточно средств на банковском счете")
        
        acc.balance = float(acc.balance) - req.initial_amount
        tx = Transaction(
            account_id=acc.id,
            category='expense',
            operation_type='transfer_out_invest',
            amount=req.initial_amount,
            description="Открытие вклада"
        )
        bdb.add(tx)
        await bdb.commit()

    # 2. Создаем вклад в базе инвестиций
    async with invest_db() as idb:
        
        dep = Deposit(
            client_id=req.client_id,
            account_number=f"4230{random.randint(1000000000000000, 9999999999999999)}",
            balance=req.initial_amount,
            interest_rate=15.0, # Мок-ставка
            term_months=req.term_months
        )
        idb.add(dep)
        await idb.commit()
        await idb.refresh(dep)
        log_success("InvestAPI", f"Вклад {dep.account_number} успешно открыт для клиента {req.client_id}")
        return to_dict(dep)

@router.post("/savings/open")
async def open_savings(req: OpenProductRequest):
    """
    Открыть накопительный счет (копилку).
    Кросс-доменная операция: Банк -> Инвестиции.
    """
    # 1. Списание из банка
    async with bank_db() as bdb:
        acc = await bdb.scalar(select(Account).where(Account.id == req.from_bank_account_id))
        if not acc: raise HTTPException(404, "Банковский счет не найден")
        if float(acc.balance) < req.initial_amount:
            raise HTTPException(400, "Недостаточно средств на банковском счете")
        
        acc.balance = float(acc.balance) - req.initial_amount
        tx = Transaction(
            account_id=acc.id,
            category='expense',
            operation_type='transfer_out_invest',
            amount=req.initial_amount,
            description="Открытие накопительного счета"
        )
        bdb.add(tx)
        await bdb.commit()

    # 2. Создание копилки
    async with invest_db() as idb:
        
        sav = SavingsAccount(
            client_id=req.client_id,
            account_number=f"4081{random.randint(1000000000000000, 9999999999999999)}",
            balance=req.initial_amount,
            interest_rate=10.0 # Мок-ставка
        )
        idb.add(sav)
        await idb.commit()
        await idb.refresh(sav)
        log_success("InvestAPI", f"Накопительный счет {sav.account_number} успешно открыт")
        return to_dict(sav)

@router.post("/broker-accounts/open")
async def open_broker_account(req: OpenProductRequest):
    """
    Открыть брокерский счет.
    Кросс-доменная операция: Банк -> Инвестиции.
    """
    # 1. Списание из банка
    async with bank_db() as bdb:
        acc = await bdb.scalar(select(Account).where(Account.id == req.from_bank_account_id))
        if not acc: raise HTTPException(404, "Банковский счет не найден")
        if float(acc.balance) < req.initial_amount:
            raise HTTPException(400, "Недостаточно средств на банковском счете")
        
        acc.balance = float(acc.balance) - req.initial_amount
        tx = Transaction(
            account_id=acc.id,
            category='expense',
            operation_type='transfer_out_broker',
            amount=req.initial_amount,
            description="Пополнение брокерского счета"
        )
        bdb.add(tx)
        await bdb.commit()

    # 2. Создание брокерского счета
    async with invest_db() as idb:
        
        broker = BrokerAccount(
            client_id=req.client_id,
            account_number=f"3060{random.randint(1000000000000000, 9999999999999999)}",
            balance=req.initial_amount
        )
        idb.add(broker)
        await idb.commit()
        await idb.refresh(broker)
        log_success("InvestAPI", f"Брокерский счет {broker.account_number} успешно открыт")
        return to_dict(broker)

@router.post("/deposits/{deposit_id}/close")
async def close_deposit(deposit_id: str, req: CloseProductRequest):
    """
    Закрыть вклад.
    Кросс-доменная операция: Инвестиции -> Банк. Баланс вклада переводится на банковский счет.
    """
    amount_to_return = 0.0
    
    async with invest_db() as idb:
        dep = await idb.scalar(select(Deposit).where(Deposit.id == deposit_id))
        if not dep: raise HTTPException(404, "Вклад не найден")
        if dep.status == "closed": raise HTTPException(400, "Вклад уже закрыт")
        
        amount_to_return = float(dep.balance)
        dep.status = "closed"
        dep.balance = 0
        await idb.commit()

    async with bank_db() as bdb:
        acc = await bdb.scalar(select(Account).where(Account.id == req.to_bank_account_id))
        if not acc: raise HTTPException(404, "Банковский счет не найден")
        
        acc.balance = float(acc.balance) + amount_to_return
        tx = Transaction(
            account_id=acc.id,
            category='income',
            operation_type='transfer_in_invest',
            amount=amount_to_return,
            description="Закрытие вклада"
        )
        bdb.add(tx)
        await bdb.commit()
    
    log_info("InvestAPI", f"Вклад {deposit_id} закрыт. На счет возвращено: {amount_to_return}")
    return {"status": "success", "returned_amount": amount_to_return}

@router.post("/savings/{savings_id}/close")
async def close_savings(savings_id: str, req: CloseProductRequest):
    """
    Закрыть накопительный счет (копилку).
    Кросс-доменная операция: Инвестиции -> Банк. Баланс переводится на банковский счет.
    """
    amount_to_return = 0.0
    
    async with invest_db() as idb:
        sav = await idb.scalar(select(SavingsAccount).where(SavingsAccount.id == savings_id))
        if not sav: raise HTTPException(404, "Накопительный счет не найден")
        if sav.status == "closed": raise HTTPException(400, "Счет уже закрыт")
        
        amount_to_return = float(sav.balance)
        sav.status = "closed"
        sav.balance = 0
        await idb.commit()

    async with bank_db() as bdb:
        acc = await bdb.scalar(select(Account).where(Account.id == req.to_bank_account_id))
        if not acc: raise HTTPException(404, "Банковский счет не найден")
        
        acc.balance = float(acc.balance) + amount_to_return
        tx = Transaction(
            account_id=acc.id,
            category='income',
            operation_type='transfer_in_invest',
            amount=amount_to_return,
            description="Закрытие накопительного счета"
        )
        bdb.add(tx)
        await bdb.commit()
    
    log_info("InvestAPI", f"Копилка {savings_id} закрыта. На счет возвращено: {amount_to_return}")
    return {"status": "success", "returned_amount": amount_to_return}

@router.post("/broker-accounts/{account_id}/strategy/subscribe")
async def subscribe_strategy(account_id: str, req: StrategySubscription):
    """
    Подключить брокерский счет к инвестиционной стратегии.
    Воркеры будут использовать эту стратегию для начисления доходности.
    """
    async with invest_db() as idb:
        acc = await idb.scalar(select(BrokerAccount).where(BrokerAccount.id == account_id))
        if not acc: raise HTTPException(404, "Брокерский счет не найден")
        
        acc.strategy_id = req.strategy_id
        await idb.commit()
        log_info("InvestAPI", f"Брокерский счет {account_id} подписан на стратегию {req.strategy_id}")
        return {"status": "success"}

@router.post("/broker-accounts/{account_id}/strategy/unsubscribe")
async def unsubscribe_strategy(account_id: str):
    """
    Отключить брокерский счет от текущей инвестиционной стратегии.
    """
    async with invest_db() as idb:
        acc = await idb.scalar(select(BrokerAccount).where(BrokerAccount.id == account_id))
        if not acc: raise HTTPException(404, "Брокерский счет не найден")
        
        acc.strategy_id = None
        await idb.commit()
        log_info("InvestAPI", f"Брокерский счет {account_id} отписан от стратегии")
        return {"status": "success"}
