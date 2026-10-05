from decimal import Decimal
from typing import Optional, List
import random
from datetime import date
from fastapi import APIRouter, HTTPException, Depends, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from pydantic import BaseModel, Field

from src.simulations.db.bank.db.client import get_async_session_maker
from src.simulations.db.bank.db.models import Account, Card, Transaction, Tariff, AutoPayment
from src.simulations.core.utils.console_logger import log_info, log_error, log_success
from src.simulations.api.core.auth import verify_bank_token
from src.simulations.core.crypto import get_bank_ca, get_receipt_signer
from src.simulations.api.schemas.responses import (
    AccountResponse,
    TariffResponse,
    CardResponse,
    TransactionResponse,
    AutoPaymentResponse,
    BankCertificatesInfoResponse,
)

router = APIRouter(dependencies=[Depends(verify_bank_token)])
pki_router = APIRouter(tags=["Bank PKI"])


@pki_router.get("/ca/certificate", response_model=BankCertificatesInfoResponse)
async def get_bank_certificate_info():
    """
    Публичный эндпоинт сертификатов Root CA и Банка для верификации Анклавом.
    """
    ca = get_bank_ca()
    return {
        "root_ca_pem": ca.get_ca_cert_pem(),
        "bank_cert_pem": ca.get_bank_cert_pem(),
        "bank_fingerprint": ca.get_bank_cert_fingerprint(),
        "signer_cn": "Financial AI Simulation Bank",
    }


@pki_router.get("/ca/root-cert.pem", response_class=PlainTextResponse)
async def get_root_ca_pem():
    """Возвращает сертификат Root CA в PEM-формате."""
    return get_bank_ca().get_ca_cert_pem()


@pki_router.get("/ca/bank-cert.pem", response_class=PlainTextResponse)
async def get_bank_cert_pem():
    """Возвращает сертификат Банка в PEM-формате."""
    return get_bank_ca().get_bank_cert_pem()


def get_db():

    """
    Фабрика для получения асинхронной сессии подключения к банковской БД.
    """
    session_maker = get_async_session_maker()
    return session_maker()

# --- Схемы данных (Pydantic) ---

class TransferRequest(BaseModel):
    """
    Схема запроса для перевода средств внутри банка.
    """
    from_account_id: str
    amount: Decimal = Field(..., gt=Decimal("0.00"), description="Сумма перевода (строго больше нуля)")
    transfer_type: str # Типы: 'account', 'phone', 'card'
    destination: str # Номер счета, номер телефона или номер карты
    description: Optional[str] = None

class AutoPaymentCreate(BaseModel):
    """
    Схема запроса для создания нового автоплатежа.
    """
    amount: Decimal = Field(..., gt=Decimal("0.00"), description="Сумма автоплатежа (строго больше нуля)")
    recipient: str
    schedule: str # Расписание: 'monthly', 'weekly'
    next_payment_date: date

# --- Эндпоинты ---

@router.get("/accounts/by-client/{client_id}", response_model=List[AccountResponse])
async def get_accounts(client_id: str) -> List[AccountResponse]:
    """
    Получить список всех счетов для указанного клиента.
    """
    async with get_db() as db:
        result = await db.execute(select(Account).where(Account.client_id == client_id))
        accounts = result.scalars().all()
        log_info("BankAPI", f"Запрошены счета клиента {client_id}. Найдено: {len(accounts)}")
        return [AccountResponse.model_validate(acc) for acc in accounts]

@router.get("/accounts/{account_id}/tariff", response_model=TariffResponse)
async def get_tariff(account_id: str) -> TariffResponse:
    """
    Получить информацию о тарифе, привязанном к конкретному счету.
    """
    async with get_db() as db:
        acc = await db.scalar(select(Account).where(Account.id == account_id))
        if not acc: 
            log_error("BankAPI", f"Счет {account_id} не найден при запросе тарифа")
            raise HTTPException(404, "Счет не найден")
            
        tariff = await db.scalar(select(Tariff).where(Tariff.id == acc.tariff_id))
        return TariffResponse.model_validate(tariff)

@router.get("/accounts/{account_id}/cards", response_model=List[CardResponse])
async def get_cards(account_id: str) -> List[CardResponse]:
    """
    Получить список всех карт, привязанных к счету.
    """
    async with get_db() as db:
        result = await db.execute(select(Card).where(Card.account_id == account_id))
        return [CardResponse.model_validate(c) for c in result.scalars().all()]

@router.get("/accounts/{account_id}/transactions", response_model=List[TransactionResponse])
async def get_transactions(account_id: str) -> List[TransactionResponse]:
    """
    Получить историю транзакций по счету, отсортированную по дате убывания.
    """
    async with get_db() as db:
        result = await db.execute(
            select(Transaction)
            .where(Transaction.account_id == account_id)
            .order_by(Transaction.date.desc())
        )
        return [TransactionResponse.model_validate(t) for t in result.scalars().all()]

@router.get("/accounts/{account_id}/autopayments", response_model=List[AutoPaymentResponse])
async def get_autopayments(account_id: str) -> List[AutoPaymentResponse]:
    """
    Получить список активных автоплатежей для счета.
    """
    async with get_db() as db:
        result = await db.execute(select(AutoPayment).where(AutoPayment.account_id == account_id))
        return [AutoPaymentResponse.model_validate(ap) for ap in result.scalars().all()]

@router.post("/accounts/{account_id}/cards/issue", response_model=CardResponse)
async def issue_card(account_id: str) -> CardResponse:
    """
    Выпустить новую карту и привязать ее к существующему счету.
    """
    async with get_db() as db:
        acc = await db.scalar(select(Account).where(Account.id == account_id))
        if not acc: 
            log_error("BankAPI", f"Попытка выпустить карту для несуществующего счета {account_id}")
            raise HTTPException(404, "Счет не найден")
        
        
        # Генерируем тестовый номер карты
        new_card = Card(
            account_id=account_id,
            card_number=f"4276{random.randint(100000000000, 999999999999)}"
        )
        db.add(new_card)
        await db.commit()
        await db.refresh(new_card)
        log_success("BankAPI", f"Успешно выпущена карта {new_card.card_number} для счета {account_id}")
        return CardResponse.model_validate(new_card)

@router.post("/cards/{card_id}/block")
async def block_card(card_id: str):
    """
    Заблокировать карту (необратимое действие в рамках симуляции, 
    для разблокировки нужен перевыпуск).
    """
    async with get_db() as db:
        card = await db.scalar(select(Card).where(Card.id == card_id))
        if not card: raise HTTPException(404, "Карта не найдена")
        
        card.status = "blocked"
        await db.commit()
        log_info("BankAPI", f"Карта {card_id} заблокирована")
        return {"status": "success", "message": "Карта заблокирована"}

@router.post("/cards/{card_id}/freeze")
async def freeze_card(card_id: str):
    """
    Заморозить карту (временная блокировка).
    """
    async with get_db() as db:
        card = await db.scalar(select(Card).where(Card.id == card_id))
        if not card: raise HTTPException(404, "Карта не найдена")
        
        card.status = "frozen"
        await db.commit()
        log_info("BankAPI", f"Карта {card_id} заморожена")
        return {"status": "success", "message": "Карта заморожена"}

@router.post("/cards/{card_id}/reissue", response_model=CardResponse)
async def reissue_card(card_id: str) -> CardResponse:
    """
    Перевыпустить карту. Старая карта блокируется, создается новая 
    и привязывается к тому же счету.
    """
    async with get_db() as db:
        card = await db.scalar(select(Card).where(Card.id == card_id))
        if not card: raise HTTPException(404, "Карта не найдена")
        
        card.status = "blocked"
        
        
        new_card = Card(
            account_id=card.account_id,
            card_number=f"4276{random.randint(100000000000, 999999999999)}"
        )
        db.add(new_card)
        await db.commit()
        await db.refresh(new_card)
        log_success("BankAPI", f"Карта {card_id} перевыпущена. Новый номер: {new_card.card_number}")
        return CardResponse.model_validate(new_card)

@router.post("/accounts/{account_id}/close")
async def close_account(
    account_id: str,
    request: Request = None,
    response: Response = None,
):
    """
    Закрыть банковский счет. 
    Требует, чтобы баланс счета был равен нулю.
    Подписывает чек-квитанцию закрытия счета приватным ключом Банка.
    """
    async with get_db() as db:
        acc = await db.scalar(
            select(Account)
            .where(Account.id == account_id)
            .with_for_update()
        )
        if not acc: raise HTTPException(404, "Счет не найден")
        
        if acc.status == "closed":
            raise HTTPException(400, "Счет уже закрыт")

        if Decimal(str(acc.balance)) != Decimal("0.00"):
            log_error("BankAPI", f"Попытка закрыть счет {account_id} с ненулевым балансом ({acc.balance})")
            raise HTTPException(400, "Невозможно закрыть счет с ненулевым балансом")
            
        acc.status = "closed"
        await db.commit()
        log_info("BankAPI", f"Счет {account_id} закрыт")

        request_nonce = None
        if request is not None:
            request_nonce = getattr(request.state, "enclave_nonce", None) or request.headers.get("X-Nonce")
        receipt = get_receipt_signer().create_and_sign_receipt(
            action="bank_account_close",
            data={"account_id": account_id, "status": "closed"},
            request_nonce=request_nonce,
        )
        if response is not None:
            response.headers["X-Bank-Signature"] = receipt["signature"]
        return {"status": "success", "signed_receipt": receipt}



@router.post("/accounts/{account_id}/autopayments", response_model=AutoPaymentResponse)
async def create_autopayment(account_id: str, data: AutoPaymentCreate) -> AutoPaymentResponse:
    """
    Создать новый автоплатеж, привязанный к счету.
    """
    async with get_db() as db:
        ap = AutoPayment(
            account_id=account_id,
            amount=data.amount,
            recipient=data.recipient,
            schedule=data.schedule,
            next_payment_date=data.next_payment_date
        )
        db.add(ap)
        await db.commit()
        await db.refresh(ap)
        log_success("BankAPI", f"Создан автоплатеж для счета {account_id} на сумму {data.amount}")
        return AutoPaymentResponse.model_validate(ap)

@router.post("/transfers")
async def transfer_money(
    data: TransferRequest,
    request: Request = None,
    response: Response = None,
):
    """
    Универсальный эндпоинт для перевода средств внутри банка.
    Поддерживает переводы по номеру счета, карты или телефона.
    Использует пессимистическую блокировку строк (SELECT FOR UPDATE) 
    в детерминированном порядке ID для защиты от состояния гонки и взаимных блокировок.
    Подписывает сформированный чек-ответ приватным ключом Банка (Блок 5 схемы arch.txt).
    """
    async with get_db() as db:
        receiver_acc = None
        
        # Поиск счета получателя в зависимости от типа перевода
        if data.transfer_type == 'account':
            receiver_acc = await db.scalar(select(Account).where(Account.account_number == data.destination))
        elif data.transfer_type == 'card':
            card = await db.scalar(select(Card).where(Card.card_number == data.destination))
            if card:
                receiver_acc = await db.scalar(select(Account).where(Account.id == card.account_id))
        elif data.transfer_type == 'phone':
            # Для симуляции предполагаем, что номер телефона совпадает с client_id
            # Выбираем первый попавшийся счет клиента
            receiver_acc = await db.scalar(select(Account).where(Account.client_id == data.destination))
        
        if not receiver_acc:
            log_error("BankAPI", f"Получатель не найден. Тип: {data.transfer_type}, Назначение: {data.destination}")
            raise HTTPException(404, "Получатель не найден")

        if str(data.from_account_id) == str(receiver_acc.id):
            log_error("BankAPI", f"Попытка самоперевода на счет {data.from_account_id}")
            raise HTTPException(400, "Перевод на тот же самый счет невозможен")

        lock_ids = sorted([str(data.from_account_id), str(receiver_acc.id)])
        accounts_res = await db.execute(
            select(Account)
            .where(Account.id.in_(lock_ids))
            .order_by(Account.id)
            .with_for_update()
        )
        accounts_map = {str(acc.id): acc for acc in accounts_res.scalars().all()}

        sender = accounts_map.get(str(data.from_account_id))
        if not sender:
            raise HTTPException(404, "Счет отправителя не найден")
            
        receiver = accounts_map.get(str(receiver_acc.id))
        if not receiver:
            raise HTTPException(404, "Счет получателя не найден")

        if sender.status and sender.status != "active":
            raise HTTPException(400, f"Счет отправителя недоступен для перевода (статус: {sender.status})")

        if receiver.status and receiver.status != "active":
            raise HTTPException(400, f"Счет получателя недоступен для перевода (статус: {receiver.status})")

        sender_balance = Decimal(str(sender.balance))
        if sender_balance < data.amount:
            log_error("BankAPI", f"Недостаточно средств на счете {sender.id} для перевода {data.amount}")
            raise HTTPException(400, "Недостаточно средств")

        # Выполнение перевода
        sender.balance = sender_balance - data.amount
        receiver.balance = Decimal(str(receiver.balance)) + data.amount

        # Запись транзакций для истории
        tx_out = Transaction(
            account_id=sender.id,
            category='expense',
            operation_type=f'transfer_out_{data.transfer_type}',
            amount=data.amount,
            description=data.description or f"Перевод получателю: {data.destination}"
        )
        tx_in = Transaction(
            account_id=receiver.id,
            category='income',
            operation_type=f'transfer_in_{data.transfer_type}',
            amount=data.amount,
            description=data.description or f"Перевод от: {sender.account_number}"
        )
        
        db.add(tx_out)
        db.add(tx_in)
        await db.commit()

        log_success("BankAPI", f"Успешный перевод {data.amount} от {sender.id} к {receiver.id}")

        # Криптографическое подписание чека приватным ключом Банка (Блок 5 схемы arch.txt)
        request_nonce = None
        if request is not None:
            request_nonce = getattr(request.state, "enclave_nonce", None) or request.headers.get("X-Nonce")
        receipt = get_receipt_signer().create_and_sign_receipt(
            action="bank_transfer",
            data={
                "transaction_id": str(tx_out.id),
                "from_account_id": str(sender.id),
                "to_account_id": str(receiver.id),
                "amount": str(data.amount),
                "transfer_type": data.transfer_type,
                "destination": data.destination,
            },
            request_nonce=request_nonce,
        )
        if response is not None:
            response.headers["X-Bank-Signature"] = receipt["signature"]
        return {
            "status": "success",
            "transaction_id": tx_out.id,
            "signed_receipt": receipt,
        }


