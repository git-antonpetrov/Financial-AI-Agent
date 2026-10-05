from decimal import Decimal
from typing import Optional, List
from datetime import datetime, timezone
import os
from fastapi import APIRouter, HTTPException, Depends, Request, Response
from sqlalchemy.future import select

from pydantic import BaseModel, Field

from src.simulations.db.digital_ruble.db.client import get_async_session_maker as get_ruble_session
from src.simulations.db.bank.db.client import get_async_session_maker as get_bank_session
from src.simulations.db.digital_ruble.db.models import Wallet, RubleTransaction, SmartContract
from src.simulations.db.bank.db.models import Account, Transaction
from src.simulations.core.utils.console_logger import log_info, log_error, log_success
from src.simulations.api.core.auth import verify_digital_token, verify_oracle_token
from src.simulations.core.crypto import (
    get_receipt_signer,
    get_digital_platform_signer,
    get_oracle_verifier,
)
from src.simulations.api.schemas.responses import (
    WalletResponse,
    RubleTransactionResponse,
    SmartContractResponse,
)

router = APIRouter(dependencies=[Depends(verify_digital_token)])


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
    amount: Decimal = Field(..., gt=Decimal("0.00"), description="Сумма операции (строго больше нуля)")

class RubleTransferRequest(BaseModel):
    """Схема запроса для P2P перевода цифровых рублей."""
    to_wallet_id: str
    amount: Decimal = Field(..., gt=Decimal("0.00"), description="Сумма P2P перевода (строго больше нуля)")

class CreateContractRequest(BaseModel):
    """Схема запроса для создания смарт-контракта."""
    receiver_wallet_id: str
    amount: Decimal = Field(..., gt=Decimal("0.00"), description="Сумма смарт-контракта (строго больше нуля)")
    condition_type: str
    contract_code: str

class ConditionUpdateRequest(BaseModel):
    """Схема запроса для обновления внешнего условия смарт-контракта оракулом."""
    status: str
    oracle_name: Optional[str] = "default_oracle"
    reason: Optional[str] = None
    signature: Optional[str] = None

# --- Вспомогательные функции компенсации (Saga pattern) ---

async def _compensate_bank_transfer(bank_account_id: str, amount: Decimal, reason: str, op_type: str = "transfer_in_compensation"):
    """
    Компенсационная транзакция: возвращает средства на банковский счет при сбое в кошельке цифрового рубля.
    """
    try:
        async with bank_db() as bdb:
            acc = await bdb.scalar(
                select(Account)
                .where(Account.id == bank_account_id)
                .with_for_update()
            )
            if acc:
                acc.balance = Decimal(str(acc.balance)) + amount
                tx = Transaction(
                    account_id=acc.id,
                    category='income',
                    operation_type=op_type,
                    amount=amount,
                    description=f"Компенсация: {reason}"
                )
                bdb.add(tx)
                await bdb.commit()
                log_info("RubleAPI", f"Успешная компенсация {amount} на счет {bank_account_id} ({reason})")
    except Exception as comp_err:
        log_error("RubleAPI", f"КРИТИЧЕСКИЙ СБОЙ КОМПЕНСАЦИИ на счете {bank_account_id}: {comp_err}")

async def _compensate_wallet_withdraw(wallet_id: str, amount: Decimal, reason: str):
    """
    Компенсационная транзакция: возвращает средства в кошелек цифрового рубля при сбое зачисления на банковский счет.
    """
    try:
        async with ruble_db() as rdb:
            wallet = await rdb.scalar(
                select(Wallet)
                .where(Wallet.id == wallet_id)
                .with_for_update()
            )
            if wallet:
                wallet.balance = Decimal(str(wallet.balance)) + amount
                await rdb.commit()
                log_info("RubleAPI", f"Успешная компенсация: возвращено {amount} в кошелек {wallet_id} ({reason})")
    except Exception as comp_err:
        log_error("RubleAPI", f"КРИТИЧЕСКИЙ СБОЙ КОМПЕНСАЦИИ кошелька {wallet_id}: {comp_err}")

# --- Эндпоинты ---

@router.get("/wallets/{client_id}", response_model=WalletResponse)
async def get_wallet(client_id: str) -> WalletResponse:
    """
    Получить кошелек цифрового рубля для указанного клиента.
    """
    async with ruble_db() as db:
        wallet = await db.scalar(select(Wallet).where(Wallet.client_id == client_id))
        if not wallet: 
            log_error("RubleAPI", f"Кошелек для клиента {client_id} не найден")
            raise HTTPException(404, "Кошелек не найден")
        return WalletResponse.model_validate(wallet)

@router.get("/wallets/{wallet_id}/transactions", response_model=List[RubleTransactionResponse])
async def get_transactions(wallet_id: str) -> List[RubleTransactionResponse]:
    """
    Получить историю транзакций для кошелька цифрового рубля.
    """
    async with ruble_db() as db:
        result = await db.execute(
            select(RubleTransaction)
            .where((RubleTransaction.sender_wallet_id == wallet_id) | (RubleTransaction.receiver_wallet_id == wallet_id))
            .order_by(RubleTransaction.timestamp.desc())
        )
        return [RubleTransactionResponse.model_validate(tx) for tx in result.scalars().all()]

@router.post("/wallets/{wallet_id}/fund")
async def fund_wallet(
    wallet_id: str,
    req: FundWithdrawRequest,
    request: Request = None,
    response: Response = None,
):
    """
    Пополнить кошелек цифрового рубля со счета в банке.
    Кросс-доменная операция: Банк -> Цифровой Рубль.
    Использует with_for_update() для блокировки счета и кошелька.
    Реализует Saga-паттерн с автоматической компенсацией на банковский счет при сбое.
    Подписывает сформированный чек-ответ приватным ключом Банка (Блок 5 схемы arch.txt).
    """
    # 1. Списание из банка
    async with bank_db() as bdb:
        acc = await bdb.scalar(
            select(Account)
            .where(Account.id == req.bank_account_id)
            .with_for_update()
        )
        if not acc: raise HTTPException(404, "Банковский счет не найден")
        
        acc_balance = Decimal(str(acc.balance))
        if acc_balance < req.amount:
            raise HTTPException(400, "Недостаточно средств на банковском счете")
        
        acc.balance = acc_balance - req.amount
        tx = Transaction(
            account_id=acc.id,
            category='expense',
            operation_type='transfer_out_ruble',
            amount=req.amount,
            description="Пополнение кошелька цифрового рубля"
        )
        bdb.add(tx)
        await bdb.commit()

    # 2. Пополнение кошелька с Saga-компенсацией
    try:
        async with ruble_db() as rdb:
            wallet = await rdb.scalar(
                select(Wallet)
                .where(Wallet.id == wallet_id)
                .with_for_update()
            )
            if not wallet: raise HTTPException(404, "Кошелек не найден")
            
            wallet.balance = Decimal(str(wallet.balance)) + req.amount
            await rdb.commit()
    except Exception as e:
        log_error("RubleAPI", f"Ошибка пополнения кошелька {wallet_id}: {e}. Запуск компенсационной транзакции...")
        await _compensate_bank_transfer(
            bank_account_id=req.bank_account_id,
            amount=req.amount,
            reason="Сбой пополнения цифрового кошелька",
            op_type="compensation_ruble_fund"
        )
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(500, f"Ошибка пополнения цифрового кошелька. Средства компенсированы на банковский счет: {e}")
        
    log_success("RubleAPI", f"Кошелек {wallet_id} пополнен на {req.amount} со счета {req.bank_account_id}")
    request_nonce = None
    if request is not None:
        request_nonce = getattr(request.state, "enclave_nonce", None) or request.headers.get("X-Nonce")
    receipt = get_receipt_signer().create_and_sign_receipt(
        action="ruble_wallet_fund",
        data={
            "wallet_id": wallet_id,
            "bank_account_id": req.bank_account_id,
            "amount": str(req.amount),
        },
        request_nonce=request_nonce,
    )
    if response is not None:
        response.headers["X-Bank-Signature"] = receipt["signature"]
    return {"status": "success", "signed_receipt": receipt}

@router.post("/wallets/{wallet_id}/withdraw")
async def withdraw_wallet(
    wallet_id: str,
    req: FundWithdrawRequest,
    request: Request = None,
    response: Response = None,
):
    """
    Вывести цифровые рубли на банковский счет.
    Кросс-доменная операция: Цифровой Рубль -> Банк.
    Использует with_for_update() для блокировки кошелька и счета.
    Реализует Saga-паттерн с автоматической компенсацией в кошелек при сбое зачисления в банк.
    Подписывает сформированный чек-ответ приватным ключом Банка (Блок 5 схемы arch.txt).
    """
    # 1. Списание из кошелька
    async with ruble_db() as rdb:
        wallet = await rdb.scalar(
            select(Wallet)
            .where(Wallet.id == wallet_id)
            .with_for_update()
        )
        if not wallet: raise HTTPException(404, "Кошелек не найден")
        
        wallet_balance = Decimal(str(wallet.balance))
        if wallet_balance < req.amount:
            raise HTTPException(400, "Недостаточно цифровых рублей")
        
        wallet.balance = wallet_balance - req.amount
        await rdb.commit()

    # 2. Зачисление в банк с Saga-компенсацией
    try:
        async with bank_db() as bdb:
            acc = await bdb.scalar(
                select(Account)
                .where(Account.id == req.bank_account_id)
                .with_for_update()
            )
            if not acc: raise HTTPException(404, "Банковский счет не найден")
            
            acc.balance = Decimal(str(acc.balance)) + req.amount
            tx = Transaction(
                account_id=acc.id,
                category='income',
                operation_type='transfer_in_ruble',
                amount=req.amount,
                description="Вывод из кошелька цифрового рубля"
            )
            bdb.add(tx)
            await bdb.commit()
    except Exception as e:
        log_error("RubleAPI", f"Ошибка зачисления при выводе из кошелька {wallet_id}: {e}. Компенсация цифровых рублей...")
        await _compensate_wallet_withdraw(
            wallet_id=wallet_id,
            amount=req.amount,
            reason="Сбой зачисления на банковский счет"
        )
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(500, f"Ошибка зачисления на банковский счет. Цифровые рубли возвращены в кошелек: {e}")
        
    log_success("RubleAPI", f"С кошелька {wallet_id} выведено {req.amount} на счет {req.bank_account_id}")
    request_nonce = None
    if request is not None:
        request_nonce = getattr(request.state, "enclave_nonce", None) or request.headers.get("X-Nonce")
    receipt = get_receipt_signer().create_and_sign_receipt(
        action="ruble_wallet_withdraw",
        data={
            "wallet_id": wallet_id,
            "bank_account_id": req.bank_account_id,
            "amount": str(req.amount),
        },
        request_nonce=request_nonce,
    )
    if response is not None:
        response.headers["X-Bank-Signature"] = receipt["signature"]
    return {"status": "success", "signed_receipt": receipt}



@router.post("/wallets/{wallet_id}/transfers", response_model=RubleTransactionResponse)
async def transfer_rubles(
    wallet_id: str,
    req: RubleTransferRequest,
    request: Request = None,
    response: Response = None,
) -> RubleTransactionResponse:
    """
    P2P перевод цифровых рублей между двумя кошельками.
    Использует пессимистическую блокировку строк (SELECT FOR UPDATE)
    в детерминированном порядке ID для защиты от race condition и deadlocks.
    Криптографически подписывает транзакцию приватным ключом платформы Банка (Блок 5 схемы arch.txt).
    """
    if str(wallet_id) == str(req.to_wallet_id):
        raise HTTPException(400, "Перевод на тот же самый кошелек невозможен")

    async with ruble_db() as db:
        lock_ids = sorted([str(wallet_id), str(req.to_wallet_id)])
        wallets_res = await db.execute(
            select(Wallet)
            .where(Wallet.id.in_(lock_ids))
            .order_by(Wallet.id)
            .with_for_update()
        )
        wallets_map = {str(w.id): w for w in wallets_res.scalars().all()}

        sender = wallets_map.get(str(wallet_id))
        if not sender: raise HTTPException(404, "Отправитель не найден")
        if sender.status and sender.status != "active":
            raise HTTPException(400, f"Кошелек отправителя недоступен (статус: {sender.status})")

        receiver = wallets_map.get(str(req.to_wallet_id))
        if not receiver: raise HTTPException(404, "Получатель не найден")
        if receiver.status and receiver.status != "active":
            raise HTTPException(400, f"Кошелек получателя недоступен (статус: {receiver.status})")

        sender_balance = Decimal(str(sender.balance))
        if sender_balance < req.amount:
            raise HTTPException(400, "Недостаточно цифровых рублей")

        sender.balance = sender_balance - req.amount
        receiver.balance = Decimal(str(receiver.balance)) + req.amount

        # Криптографическая подпись транзакции ключом платформы ЦР
        sig_ts = int(datetime.now(timezone.utc).timestamp())
        sig = get_digital_platform_signer().sign_transaction(
            sender_id=sender.id,
            receiver_id=receiver.id,
            amount=req.amount,
            contract_id="direct_p2p",
            timestamp=sig_ts,
        )

        rtx = RubleTransaction(
            sender_wallet_id=sender.id,
            receiver_wallet_id=receiver.id,
            amount=req.amount,
            signature=sig,
        )
        db.add(rtx)
        await db.commit()
        await db.refresh(rtx)
        
        if response is not None:
            response.headers["X-Bank-Signature"] = sig
        log_success("RubleAPI", f"P2P Перевод ЦР: {req.amount} от {wallet_id} к {req.to_wallet_id} (sig={sig[:16]}...)")
        return RubleTransactionResponse.model_validate(rtx)


@router.post("/wallets/{wallet_id}/smart-contracts", response_model=SmartContractResponse)
async def create_smart_contract(wallet_id: str, req: CreateContractRequest) -> SmartContractResponse:
    """
    Создать смарт-контракт. Средства замораживаются на счету инициатора 
    до момента исполнения контракта.
    Использует with_for_update() для защиты баланса создателя от гонки.
    """
    if str(wallet_id) == str(req.receiver_wallet_id):
        raise HTTPException(400, "Создатель и получатель смарт-контракта не могут совпадать")

    async with ruble_db() as db:
        creator = await db.scalar(
            select(Wallet)
            .where(Wallet.id == wallet_id)
            .with_for_update()
        )
        if not creator: raise HTTPException(404, "Создатель контракта не найден")
        if creator.status and creator.status != "active":
            raise HTTPException(400, f"Кошелек создателя недоступен (статус: {creator.status})")
        
        creator_balance = Decimal(str(creator.balance))
        if creator_balance < req.amount:
            raise HTTPException(400, "Недостаточно средств для заморозки под контракт")
        
        # Замораживаем средства
        creator.balance = creator_balance - req.amount
        creator.frozen_balance = Decimal(str(creator.frozen_balance)) + req.amount

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
        return SmartContractResponse.model_validate(contract)

@router.get("/wallets/{wallet_id}/smart-contracts", response_model=List[SmartContractResponse])
async def get_smart_contracts(wallet_id: str) -> List[SmartContractResponse]:
    """
    Получить список смарт-контрактов, связанных с кошельком (как инициатор или получатель).
    """
    async with ruble_db() as db:
        result = await db.execute(
            select(SmartContract)
            .where((SmartContract.creator_wallet_id == wallet_id) | (SmartContract.receiver_wallet_id == wallet_id))
        )
        return [SmartContractResponse.model_validate(c) for c in result.scalars().all()]

@router.post("/smart-contracts/{contract_id}/condition", dependencies=[Depends(verify_oracle_token)])
async def update_contract_condition(
    contract_id: str,
    req: ConditionUpdateRequest,
    request: Request = None,
    response: Response = None,
):
    """
    Оракул: доверенное обновление внешнего условия смарт-контракта.
    Доступ разрешен только доверенному оракулу (ORACLE_BOOTSTRAP_TOKEN),
    агенту цифрового рубля или главному оркестратору.
    Выполняет криптографическую проверку цифровой подписи оракула (Блок 5 схемы arch.txt).
    """
    if req.status not in ['fulfilled', 'failed']:
        raise HTTPException(400, "Неверный статус условия. Разрешены только 'fulfilled' или 'failed'")

    # Криптографическая валидация подписи оракула
    if req.signature:
        is_valid, sig_err = get_oracle_verifier().verify_condition_signature(
            contract_id=contract_id,
            status=req.status,
            oracle_name=req.oracle_name,
            signature_b64=req.signature,
        )
        if not is_valid:
            log_error("RubleAPI", f"Недействительная цифровая подпись оракула для контракта {contract_id}: {sig_err}")
            raise HTTPException(403, detail=f"Недействительная цифровая подпись оракула: {sig_err}")
    elif os.getenv("REQUIRE_ORACLE_SIGNATURE", "false").lower() == "true":
        raise HTTPException(401, detail="Требуется цифровая подпись оракула")
    
    async with ruble_db() as db:
        contract = await db.scalar(
            select(SmartContract)
            .where(SmartContract.id == contract_id)
            .with_for_update()
        )
        if not contract:
            log_error("RubleAPI", f"Оракул запросил несуществующий контракт {contract_id}")
            raise HTTPException(404, "Смарт-контракт не найден")
            
        if contract.status != "active":
            raise HTTPException(400, f"Контракт не активен (текущий статус: {contract.status})")

        if contract.condition_status in ['fulfilled', 'failed']:
            raise HTTPException(409, f"Условие контракта уже зафиксировано со статусом '{contract.condition_status}'")
        
        contract.condition_status = req.status
        await db.commit()
        await db.refresh(contract)
        
        log_info(
            "RubleAPI",
            f"Оракул '{req.oracle_name}' обновил статус условия контракта {contract_id} на '{req.status}' "
            f"(причина: {req.reason or 'не указана'})"
        )

        request_nonce = None
        if request is not None:
            request_nonce = getattr(request.state, "enclave_nonce", None) or request.headers.get("X-Nonce")
        receipt = get_receipt_signer().create_and_sign_receipt(
            action="ruble_condition_update",
            data={
                "contract_id": contract_id,
                "new_condition_status": req.status,
                "oracle_name": req.oracle_name,
            },
            request_nonce=request_nonce,
        )
        if response is not None:
            response.headers["X-Bank-Signature"] = receipt["signature"]
        return {
            "status": "success",
            "contract_id": contract_id,
            "new_condition_status": req.status,
            "oracle_name": req.oracle_name,
            "signed_receipt": receipt,
        }

