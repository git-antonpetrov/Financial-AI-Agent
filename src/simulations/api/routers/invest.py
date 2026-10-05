from decimal import Decimal
from typing import Optional, List
from datetime import datetime
from dateutil.relativedelta import relativedelta
import random
from fastapi import APIRouter, HTTPException, Depends, Request, Response
from sqlalchemy.future import select
from pydantic import BaseModel, Field

from src.simulations.db.invest.db.client import get_async_session_maker as get_invest_session
from src.simulations.db.bank.db.client import get_async_session_maker as get_bank_session
from src.simulations.db.invest.db.models import InvestmentStrategy, SavingsAccount, Deposit, BrokerAccount
from src.simulations.db.bank.db.models import Account, Transaction
from src.simulations.core.utils.console_logger import log_info, log_error, log_success
from src.simulations.api.core.auth import verify_invest_token
from src.simulations.core.crypto import get_receipt_signer
from src.simulations.api.schemas.responses import (
    InvestmentStrategyResponse,
    SavingsAccountResponse,
    DepositResponse,
    BrokerAccountResponse,
    PortfolioResponse,
)

router = APIRouter(dependencies=[Depends(verify_invest_token)])


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
    initial_amount: Decimal = Field(..., gt=Decimal("0.00"), description="Начальная сумма (строго больше нуля)")
    term_months: Optional[int] = Field(None, gt=0, description="Срок вклада в месяцах (строго больше нуля)")

class StrategySubscription(BaseModel):
    """Схема для управления подпиской на инвестиционную стратегию."""
    strategy_id: Optional[str] = None

class CloseProductRequest(BaseModel):
    """Схема для закрытия продукта."""
    to_bank_account_id: str

# --- Вспомогательные функции компенсации (Saga pattern) ---

async def _compensate_bank_transfer(bank_account_id: str, amount: Decimal, reason: str, op_type: str = "transfer_in_compensation"):
    """
    Компенсационная транзакция: возвращает списанные средства на банковский счет в случае сбоя целевой системы.
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
                log_info("InvestAPI", f"Успешная компенсация {amount} на счет {bank_account_id} ({reason})")
    except Exception as comp_err:
        log_error("InvestAPI", f"КРИТИЧЕСКИЙ СБОЙ КОМПЕНСАЦИИ на счете {bank_account_id}: {comp_err}")

async def _compensate_close_deposit(deposit_id: str, amount: Decimal):
    """
    Компенсационная транзакция: восстанавливает статус и баланс вклада при сбое зачисления в банк.
    """
    try:
        async with invest_db() as idb:
            dep = await idb.scalar(
                select(Deposit)
                .where(Deposit.id == deposit_id)
                .with_for_update()
            )
            if dep:
                dep.status = "active"
                dep.balance = amount
                await idb.commit()
                log_info("InvestAPI", f"Успешная компенсация: восстановлен статус и баланс вклада {deposit_id}")
    except Exception as comp_err:
        log_error("InvestAPI", f"КРИТИЧЕСКИЙ СБОЙ ВОССТАНОВЛЕНИЯ вклада {deposit_id}: {comp_err}")

async def _compensate_close_savings(savings_id: str, amount: Decimal):
    """
    Компенсационная транзакция: восстанавливает статус и баланс накопительного счета при сбое зачисления в банк.
    """
    try:
        async with invest_db() as idb:
            sav = await idb.scalar(
                select(SavingsAccount)
                .where(SavingsAccount.id == savings_id)
                .with_for_update()
            )
            if sav:
                sav.status = "active"
                sav.balance = amount
                await idb.commit()
                log_info("InvestAPI", f"Успешная компенсация: восстановлен статус и баланс копилки {savings_id}")
    except Exception as comp_err:
        log_error("InvestAPI", f"КРИТИЧЕСКИЙ СБОЙ ВОССТАНОВЛЕНИЯ копилки {savings_id}: {comp_err}")

# --- Эндпоинты ---

@router.get("/strategies", response_model=List[InvestmentStrategyResponse])
async def get_strategies() -> List[InvestmentStrategyResponse]:
    """
    Получить список всех доступных инвестиционных стратегий.
    """
    async with invest_db() as db:
        result = await db.execute(select(InvestmentStrategy))
        return [InvestmentStrategyResponse.model_validate(s) for s in result.scalars().all()]

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

@router.get("/portfolio/{client_id}", response_model=PortfolioResponse)
async def get_portfolio(client_id: str) -> PortfolioResponse:
    """
    Получить сводный инвестиционный портфель клиента (копилки, вклады, брокерские счета).
    """
    async with invest_db() as db:
        savings = (await db.execute(select(SavingsAccount).where(SavingsAccount.client_id == client_id))).scalars().all()
        deposits = (await db.execute(select(Deposit).where(Deposit.client_id == client_id))).scalars().all()
        brokers = (await db.execute(select(BrokerAccount).where(BrokerAccount.client_id == client_id))).scalars().all()
        
        log_info("InvestAPI", f"Собран портфель для клиента {client_id}")
        return PortfolioResponse(
            savings_accounts=[SavingsAccountResponse.model_validate(s) for s in savings],
            deposits=[DepositResponse.model_validate(d) for d in deposits],
            broker_accounts=[BrokerAccountResponse.model_validate(b) for b in brokers]
        )

@router.post("/deposits/open", response_model=DepositResponse)
async def open_deposit(
    req: OpenProductRequest,
    request: Request = None,
    response: Response = None,
) -> DepositResponse:
    """
    Открыть новый вклад. 
    Кросс-доменная операция: списывает средства со счета в банке и создает актив в инвестициях.
    Реализует Saga-паттерн с автоматической компенсацией при сбое на шаге создания актива.
    """
    if not req.term_months:
        log_error("InvestAPI", "Попытка открыть вклад без указания срока (term_months)")
        raise HTTPException(400, "Для вклада необходимо указать срок (term_months)")
    
    # 1. Списываем средства со счета в банке
    async with bank_db() as bdb:
        acc = await bdb.scalar(
            select(Account)
            .where(Account.id == req.from_bank_account_id)
            .with_for_update()
        )
        if not acc: raise HTTPException(404, "Банковский счет не найден")
        
        acc_balance = Decimal(str(acc.balance))
        if acc_balance < req.initial_amount:
            log_error("InvestAPI", f"Недостаточно средств на банковском счете {req.from_bank_account_id} для открытия вклада")
            raise HTTPException(400, "Недостаточно средств на банковском счете")
        
        acc.balance = acc_balance - req.initial_amount
        tx = Transaction(
            account_id=acc.id,
            category='expense',
            operation_type='transfer_out_invest',
            amount=req.initial_amount,
            description="Открытие вклада"
        )
        bdb.add(tx)
        await bdb.commit()

    # 2. Создаем вклад в базе инвестиций с Saga-компенсацией
    try:
        async with invest_db() as idb:
            interest_rate = Decimal("15.00") # Мок-ставка
            monthly_rate = interest_rate / Decimal("100") / Decimal("12")
            first_payment = round(req.initial_amount * monthly_rate, 2)
            next_pay_date = datetime.now() + relativedelta(months=1)

            dep = Deposit(
                client_id=req.client_id,
                account_number=f"4230{random.randint(1000000000000000, 9999999999999999)}",
                balance=req.initial_amount,
                interest_rate=interest_rate,
                term_months=req.term_months,
                next_payment_date=next_pay_date,
                next_payment_amount=first_payment
            )
            idb.add(dep)
            await idb.commit()
            await idb.refresh(dep)
            log_success("InvestAPI", f"Вклад {dep.account_number} успешно открыт для клиента {req.client_id}")

            request_nonce = None
            if request is not None:
                request_nonce = getattr(request.state, "enclave_nonce", None) or request.headers.get("X-Nonce")
            receipt = get_receipt_signer().create_and_sign_receipt(
                action="invest_deposit_open",
                data={
                    "client_id": req.client_id,
                    "amount": str(req.initial_amount),
                    "term_months": req.term_months,
                    "deposit_id": str(dep.id),
                    "account_number": dep.account_number,
                },
                request_nonce=request_nonce,
            )
            if response is not None:
                response.headers["X-Bank-Signature"] = receipt["signature"]
                response.headers["X-Receipt-ID"] = receipt["receipt"]["receipt_id"]
            return DepositResponse.model_validate(dep)


    except Exception as e:
        log_error("InvestAPI", f"Ошибка при открытии вклада: {e}. Запуск компенсационной транзакции...")
        await _compensate_bank_transfer(
            bank_account_id=req.from_bank_account_id,
            amount=req.initial_amount,
            reason="Сбой создания вклада",
            op_type="compensation_invest_deposit"
        )
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(500, f"Ошибка создания вклада. Средства компенсированы на банковский счет: {e}")

@router.post("/savings/open", response_model=SavingsAccountResponse)
async def open_savings(
    req: OpenProductRequest,
    request: Request = None,
    response: Response = None,
) -> SavingsAccountResponse:
    """
    Открыть накопительный счет (копилку).
    Кросс-доменная операция: Банк -> Инвестиции.
    Реализует Saga-паттерн с автоматической компенсацией при сбое на шаге создания актива.
    """
    # 1. Списание из банка
    async with bank_db() as bdb:
        acc = await bdb.scalar(
            select(Account)
            .where(Account.id == req.from_bank_account_id)
            .with_for_update()
        )
        if not acc: raise HTTPException(404, "Банковский счет не найден")
        
        acc_balance = Decimal(str(acc.balance))
        if acc_balance < req.initial_amount:
            raise HTTPException(400, "Недостаточно средств на банковском счете")
        
        acc.balance = acc_balance - req.initial_amount
        tx = Transaction(
            account_id=acc.id,
            category='expense',
            operation_type='transfer_out_invest',
            amount=req.initial_amount,
            description="Открытие накопительного счета"
        )
        bdb.add(tx)
        await bdb.commit()

    # 2. Создание копилки с Saga-компенсацией
    try:
        async with invest_db() as idb:
            interest_rate = Decimal("10.00") # Мок-ставка
            monthly_rate = interest_rate / Decimal("100") / Decimal("12")
            first_payment = round(req.initial_amount * monthly_rate, 2)
            next_pay_date = datetime.now() + relativedelta(months=1)

            sav = SavingsAccount(
                client_id=req.client_id,
                account_number=f"4081{random.randint(1000000000000000, 9999999999999999)}",
                balance=req.initial_amount,
                interest_rate=interest_rate,
                next_payment_date=next_pay_date,
                next_payment_amount=first_payment
            )
            idb.add(sav)
            await idb.commit()
            await idb.refresh(sav)
            log_success("InvestAPI", f"Накопительный счет {sav.account_number} успешно открыт")

            request_nonce = None
            if request is not None:
                request_nonce = getattr(request.state, "enclave_nonce", None) or request.headers.get("X-Nonce")
            receipt = get_receipt_signer().create_and_sign_receipt(
                action="invest_savings_open",
                data={
                    "client_id": req.client_id,
                    "amount": str(req.initial_amount),
                    "savings_id": str(sav.id),
                    "account_number": sav.account_number,
                },
                request_nonce=request_nonce,
            )
            if response is not None:
                response.headers["X-Bank-Signature"] = receipt["signature"]
                response.headers["X-Receipt-ID"] = receipt["receipt"]["receipt_id"]
            return SavingsAccountResponse.model_validate(sav)
    except Exception as e:
        log_error("InvestAPI", f"Ошибка при открытии накопительного счета: {e}. Запуск компенсационной транзакции...")
        await _compensate_bank_transfer(
            bank_account_id=req.from_bank_account_id,
            amount=req.initial_amount,
            reason="Сбой создания накопительного счета",
            op_type="compensation_invest_savings"
        )
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(500, f"Ошибка создания накопительного счета. Средства компенсированы на банковский счет: {e}")

@router.post("/broker-accounts/open", response_model=BrokerAccountResponse)
async def open_broker_account(
    req: OpenProductRequest,
    request: Request = None,
    response: Response = None,
) -> BrokerAccountResponse:
    """
    Открыть брокерский счет.
    Кросс-доменная операция: Банк -> Инвестиции.
    Реализует Saga-паттерн с автоматической компенсацией при сбое на шаге создания актива.
    """
    # 1. Списание из банка
    async with bank_db() as bdb:
        acc = await bdb.scalar(
            select(Account)
            .where(Account.id == req.from_bank_account_id)
            .with_for_update()
        )
        if not acc: raise HTTPException(404, "Банковский счет не найден")
        
        acc_balance = Decimal(str(acc.balance))
        if acc_balance < req.initial_amount:
            raise HTTPException(400, "Недостаточно средств на банковском счете")
        
        acc.balance = acc_balance - req.initial_amount
        tx = Transaction(
            account_id=acc.id,
            category='expense',
            operation_type='transfer_out_broker',
            amount=req.initial_amount,
            description="Пополнение брокерского счета"
        )
        bdb.add(tx)
        await bdb.commit()

    # 2. Создание брокерского счета с Saga-компенсацией
    try:
        async with invest_db() as idb:
            next_comm_date = datetime.now() + relativedelta(months=1)

            broker = BrokerAccount(
                client_id=req.client_id,
                account_number=f"3060{random.randint(1000000000000000, 9999999999999999)}",
                balance=req.initial_amount,
                next_commission_date=next_comm_date
            )
            idb.add(broker)
            await idb.commit()
            await idb.refresh(broker)
            log_success("InvestAPI", f"Брокерский счет {broker.account_number} успешно открыт")

            request_nonce = None
            if request is not None:
                request_nonce = getattr(request.state, "enclave_nonce", None) or request.headers.get("X-Nonce")
            receipt = get_receipt_signer().create_and_sign_receipt(
                action="invest_broker_open",
                data={
                    "client_id": req.client_id,
                    "amount": str(req.initial_amount),
                    "broker_id": str(broker.id),
                    "account_number": broker.account_number,
                },
                request_nonce=request_nonce,
            )
            if response is not None:
                response.headers["X-Bank-Signature"] = receipt["signature"]
                response.headers["X-Receipt-ID"] = receipt["receipt"]["receipt_id"]
            return BrokerAccountResponse.model_validate(broker)
    except Exception as e:
        log_error("InvestAPI", f"Ошибка при открытии брокерского счета: {e}. Запуск компенсационной транзакции...")
        await _compensate_bank_transfer(
            bank_account_id=req.from_bank_account_id,
            amount=req.initial_amount,
            reason="Сбой создания брокерского счета",
            op_type="compensation_invest_broker"
        )
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(500, f"Ошибка создания брокерского счета. Средства компенсированы на банковский счет: {e}")



@router.post("/deposits/{deposit_id}/close")
async def close_deposit(
    deposit_id: str,
    req: CloseProductRequest,
    request: Request = None,
    response: Response = None,
):
    """
    Закрыть вклад.
    Кросс-доменная операция: Инвестиции -> Банк. Баланс вклада переводится на банковский счет.
    Реализует Saga-паттерн с компенсацией восстановления вклада в случае сбоя зачисления в банк.
    Подписывает сформированный чек-ответ приватным ключом Банка (Блок 5 схемы arch.txt).
    """
    amount_to_return = Decimal("0.00")
    
    async with invest_db() as idb:
        dep = await idb.scalar(
            select(Deposit)
            .where(Deposit.id == deposit_id)
            .with_for_update()
        )
        if not dep: raise HTTPException(404, "Вклад не найден")
        if dep.status == "closed": raise HTTPException(400, "Вклад уже закрыт")
        
        amount_to_return = Decimal(str(dep.balance))
        dep.status = "closed"
        dep.balance = Decimal("0.00")
        await idb.commit()

    try:
        async with bank_db() as bdb:
            acc = await bdb.scalar(
                select(Account)
                .where(Account.id == req.to_bank_account_id)
                .with_for_update()
            )
            if not acc: raise HTTPException(404, "Банковский счет не найден")
            
            acc.balance = Decimal(str(acc.balance)) + amount_to_return
            tx = Transaction(
                account_id=acc.id,
                category='income',
                operation_type='transfer_in_invest',
                amount=amount_to_return,
                description="Закрытие вклада"
            )
            bdb.add(tx)
            await bdb.commit()
    except Exception as e:
        log_error("InvestAPI", f"Ошибка зачисления при закрытии вклада {deposit_id}: {e}. Компенсация статуса вклада...")
        await _compensate_close_deposit(deposit_id, amount_to_return)
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(500, f"Ошибка зачисления средств в банк. Вклад восстановлен: {e}")
    
    log_info("InvestAPI", f"Вклад {deposit_id} закрыт. На счет возвращено: {amount_to_return}")
    request_nonce = None
    if request is not None:
        request_nonce = getattr(request.state, "enclave_nonce", None) or request.headers.get("X-Nonce")
    receipt = get_receipt_signer().create_and_sign_receipt(
        action="invest_deposit_close",
        data={
            "deposit_id": deposit_id,
            "returned_amount": str(amount_to_return),
            "to_bank_account_id": req.to_bank_account_id,
        },
        request_nonce=request_nonce,
    )
    if response is not None:
        response.headers["X-Bank-Signature"] = receipt["signature"]
    return {
        "status": "success",
        "returned_amount": amount_to_return,
        "signed_receipt": receipt,
    }

@router.post("/savings/{savings_id}/close")
async def close_savings(
    savings_id: str,
    req: CloseProductRequest,
    request: Request = None,
    response: Response = None,
):
    """
    Закрыть накопительный счет (копилку).
    Кросс-доменная операция: Инвестиции -> Банк. Баланс переводится на банковский счет.
    Реализует Saga-паттерн с компенсацией восстановления копилки в случае сбоя зачисления в банк.
    Подписывает сформированный чек-ответ приватным ключом Банка (Блок 5 схемы arch.txt).
    """
    amount_to_return = Decimal("0.00")
    
    async with invest_db() as idb:
        sav = await idb.scalar(
            select(SavingsAccount)
            .where(SavingsAccount.id == savings_id)
            .with_for_update()
        )
        if not sav: raise HTTPException(404, "Накопительный счет не найден")
        if sav.status == "closed": raise HTTPException(400, "Счет уже закрыт")
        
        amount_to_return = Decimal(str(sav.balance))
        sav.status = "closed"
        sav.balance = Decimal("0.00")
        await idb.commit()

    try:
        async with bank_db() as bdb:
            acc = await bdb.scalar(
                select(Account)
                .where(Account.id == req.to_bank_account_id)
                .with_for_update()
            )
            if not acc: raise HTTPException(404, "Банковский счет не найден")
            
            acc.balance = Decimal(str(acc.balance)) + amount_to_return
            tx = Transaction(
                account_id=acc.id,
                category='income',
                operation_type='transfer_in_invest',
                amount=amount_to_return,
                description="Закрытие накопительного счета"
            )
            bdb.add(tx)
            await bdb.commit()
    except Exception as e:
        log_error("InvestAPI", f"Ошибка зачисления при закрытии копилки {savings_id}: {e}. Компенсация статуса накопительного счета...")
        await _compensate_close_savings(savings_id, amount_to_return)
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(500, f"Ошибка зачисления средств в банк. Копилка восстановлена: {e}")
    
    log_info("InvestAPI", f"Копилка {savings_id} закрыта. На счет возвращено: {amount_to_return}")
    request_nonce = None
    if request is not None:
        request_nonce = getattr(request.state, "enclave_nonce", None) or request.headers.get("X-Nonce")
    receipt = get_receipt_signer().create_and_sign_receipt(
        action="invest_savings_close",
        data={
            "savings_id": savings_id,
            "returned_amount": str(amount_to_return),
            "to_bank_account_id": req.to_bank_account_id,
        },
        request_nonce=request_nonce,
    )
    if response is not None:
        response.headers["X-Bank-Signature"] = receipt["signature"]
    return {
        "status": "success",
        "returned_amount": amount_to_return,
        "signed_receipt": receipt,
    }



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
        if not acc.next_commission_date:
            acc.next_commission_date = datetime.now() + relativedelta(months=1)
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
