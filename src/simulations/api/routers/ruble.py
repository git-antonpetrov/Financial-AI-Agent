# Removed unused imports
# pyrefly: ignore [missing-import]
from fastapi import APIRouter, HTTPException
from sqlalchemy.future import select
from pydantic import BaseModel

from src.simulations.db.digital_ruble.db.client import get_async_session_maker as get_ruble_session
from src.simulations.db.bank.db.client import get_async_session_maker as get_bank_session
from src.simulations.db.digital_ruble.db.models import Wallet, RubleTransaction, SmartContract
from src.simulations.db.bank.db.models import Account, Transaction
from src.simulations.core.utils.console_logger import log_info, log_error, log_success
from src.simulations.api.core.utils import to_dict, to_dict_list

router = APIRouter()

def ruble_db():
    """Фабрика для получения сессии БД цифрового рубля."""
    return get_ruble_session()()

def bank_db():
    """Фабрика для получения сессии банковской БД."""
    return get_bank_session()()

# --- Схемы данных (Pydantic) ---

class FundWithdrawRequest(BaseModel):
    """Схема запроса для пополнения и вывода средств из кошелька цифрового рубля."""
    bank_account_id: str
    amount: float

class RubleTransferRequest(BaseModel):
    """Схема запроса для P2P перевода цифровых рублей."""
    to_wallet_id: str
    amount: float

class CreateContractRequest(BaseModel):
    """Схема запроса для создания смарт-контракта."""
    receiver_wallet_id: str
    amount: float
    condition_type: str
    contract_code: str

# --- Эндпоинты ---

@router.get("/wallets/{client_id}")
async def get_wallet(client_id: str):
    """
    Получить кошелек цифрового рубля для указанного клиента.
    """
    async with ruble_db() as db:
        wallet = await db.scalar(select(Wallet).where(Wallet.client_id == client_id))
        if not wallet: 
            log_error("RubleAPI", f"Кошелек для клиента {client_id} не найден")
            raise HTTPException(404, "Кошелек не найден")
        return to_dict(wallet)

@router.get("/wallets/{wallet_id}/transactions")
async def get_transactions(wallet_id: str):
    """
    Получить историю транзакций для кошелька цифрового рубля.
    """
    async with ruble_db() as db:
        result = await db.execute(
            select(RubleTransaction)
            .where((RubleTransaction.sender_wallet_id == wallet_id) | (RubleTransaction.receiver_wallet_id == wallet_id))
            .order_by(RubleTransaction.timestamp.desc())
        )
        return to_dict_list(result.scalars().all())

@router.post("/wallets/{wallet_id}/fund")
async def fund_wallet(wallet_id: str, req: FundWithdrawRequest):
    """
    Пополнить кошелек цифрового рубля со счета в банке.
    Кросс-доменная операция: Банк -> Цифровой Рубль.
    """
    # Списание из банка
    async with bank_db() as bdb:
        acc = await bdb.scalar(select(Account).where(Account.id == req.bank_account_id))
        if not acc: raise HTTPException(404, "Банковский счет не найден")
        if float(acc.balance) < req.amount:
            raise HTTPException(400, "Недостаточно средств на банковском счете")
        
        acc.balance = float(acc.balance) - req.amount
        tx = Transaction(
            account_id=acc.id,
            category='expense',
            operation_type='transfer_out_ruble',
            amount=req.amount,
            description="Пополнение кошелька цифрового рубля"
        )
        bdb.add(tx)
        await bdb.commit()

    # Пополнение кошелька
    async with ruble_db() as rdb:
        wallet = await rdb.scalar(select(Wallet).where(Wallet.id == wallet_id))
        if not wallet: raise HTTPException(404, "Кошелек не найден")
        
        wallet.balance = float(wallet.balance) + req.amount
        await rdb.commit()
        
    log_success("RubleAPI", f"Кошелек {wallet_id} пополнен на {req.amount} со счета {req.bank_account_id}")
    return {"status": "success"}

@router.post("/wallets/{wallet_id}/withdraw")
async def withdraw_wallet(wallet_id: str, req: FundWithdrawRequest):
    """
    Вывести цифровые рубли на банковский счет.
    Кросс-доменная операция: Цифровой Рубль -> Банк.
    """
    # Списание из кошелька
    async with ruble_db() as rdb:
        wallet = await rdb.scalar(select(Wallet).where(Wallet.id == wallet_id))
        if not wallet: raise HTTPException(404, "Кошелек не найден")
        if float(wallet.balance) < req.amount:
            raise HTTPException(400, "Недостаточно цифровых рублей")
        
        wallet.balance = float(wallet.balance) - req.amount
        await rdb.commit()

    # Зачисление в банк
    async with bank_db() as bdb:
        acc = await bdb.scalar(select(Account).where(Account.id == req.bank_account_id))
        if not acc: raise HTTPException(404, "Банковский счет не найден")
        
        acc.balance = float(acc.balance) + req.amount
        tx = Transaction(
            account_id=acc.id,
            category='income',
            operation_type='transfer_in_ruble',
            amount=req.amount,
            description="Вывод из кошелька цифрового рубля"
        )
        bdb.add(tx)
        await bdb.commit()
        
    log_success("RubleAPI", f"С кошелька {wallet_id} выведено {req.amount} на счет {req.bank_account_id}")
    return {"status": "success"}

@router.post("/wallets/{wallet_id}/transfers")
async def transfer_rubles(wallet_id: str, req: RubleTransferRequest):
    """
    P2P перевод цифровых рублей между двумя кошельками.
    """
    async with ruble_db() as db:
        sender = await db.scalar(select(Wallet).where(Wallet.id == wallet_id))
        if not sender: raise HTTPException(404, "Отправитель не найден")
        if float(sender.balance) < req.amount:
            raise HTTPException(400, "Недостаточно цифровых рублей")

        receiver = await db.scalar(select(Wallet).where(Wallet.id == req.to_wallet_id))
        if not receiver: raise HTTPException(404, "Получатель не найден")

        sender.balance = float(sender.balance) - req.amount
        receiver.balance = float(receiver.balance) + req.amount

        rtx = RubleTransaction(
            sender_wallet_id=sender.id,
            receiver_wallet_id=receiver.id,
            amount=req.amount
        )
        db.add(rtx)
        await db.commit()
        await db.refresh(rtx)
        
        log_success("RubleAPI", f"P2P Перевод ЦР: {req.amount} от {wallet_id} к {req.to_wallet_id}")
        return to_dict(rtx)

@router.post("/wallets/{wallet_id}/smart-contracts")
async def create_smart_contract(wallet_id: str, req: CreateContractRequest):
    """
    Создать смарт-контракт. Средства замораживаются на счету инициатора 
    до момента исполнения контракта.
    """
    async with ruble_db() as db:
        creator = await db.scalar(select(Wallet).where(Wallet.id == wallet_id))
        if not creator: raise HTTPException(404, "Создатель контракта не найден")
        if float(creator.balance) < req.amount:
            raise HTTPException(400, "Недостаточно средств для заморозки под контракт")
        
        # Замораживаем средства
        creator.balance = float(creator.balance) - req.amount
        creator.frozen_balance = float(creator.frozen_balance) + req.amount

        contract = SmartContract(
            creator_wallet_id=creator.id,
            receiver_wallet_id=req.receiver_wallet_id,
            amount=req.amount,
            condition_type=req.condition_type,
            contract_code=req.contract_code
        )
        db.add(contract)
        await db.commit()
        await db.refresh(contract)
        
        log_info("RubleAPI", f"Создан смарт-контракт от {wallet_id} для {req.receiver_wallet_id} на {req.amount}")
        return to_dict(contract)

@router.get("/wallets/{wallet_id}/smart-contracts")
async def get_smart_contracts(wallet_id: str):
    """
    Получить список смарт-контрактов, связанных с кошельком (как инициатор или получатель).
    """
    async with ruble_db() as db:
        result = await db.execute(
            select(SmartContract)
            .where((SmartContract.creator_wallet_id == wallet_id) | (SmartContract.receiver_wallet_id == wallet_id))
        )
        return to_dict_list(result.scalars().all())

@router.post("/smart-contracts/{contract_id}/condition")
async def update_contract_condition(contract_id: str, status: str):
    """
    Оракул: обновить внешнее условие контракта.
    Статус должен быть 'fulfilled' или 'failed'. После этого воркер (ruble_eod) 
    исполнит контракт ночью.
    """
    if status not in ['fulfilled', 'failed']:
        raise HTTPException(400, "Неверный статус. Используйте 'fulfilled' или 'failed'")
    
    async with ruble_db() as db:
        contract = await db.scalar(select(SmartContract).where(SmartContract.id == contract_id))
        if not contract: raise HTTPException(404, "Контракт не найден")
        if contract.status != "active":
            raise HTTPException(400, f"Контракт не активен (текущий статус: {contract.status})")
        
        contract.condition_status = status
        await db.commit()
        
        log_info("RubleAPI", f"Статус условия смарт-контракта {contract_id} обновлен оракулом на: {status}")
        return {"status": "success", "new_condition_status": status}
