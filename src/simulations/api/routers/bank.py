from typing import Optional
import random
from datetime import date
# pyrefly: ignore [missing-import]
from fastapi import APIRouter, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from pydantic import BaseModel

from src.simulations.db.bank.db.client import get_async_session_maker
from src.simulations.db.bank.db.models import Account, Card, Transaction, Tariff, AutoPayment
from src.simulations.core.utils.console_logger import log_info, log_error, log_success
from src.simulations.api.core.utils import to_dict, to_dict_list

router = APIRouter()

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
    amount: float
    transfer_type: str # Типы: 'account', 'phone', 'card'
    destination: str # Номер счета, номер телефона или номер карты
    description: Optional[str] = None

class AutoPaymentCreate(BaseModel):
    """
    Схема запроса для создания нового автоплатежа.
    """
    amount: float
    recipient: str
    schedule: str # Расписание: 'monthly', 'weekly'
    next_payment_date: date

# --- Эндпоинты ---

@router.get("/accounts/by-client/{client_id}")
async def get_accounts(client_id: str):
    """
    Получить список всех счетов для указанного клиента.
    """
    async with get_db() as db:
        result = await db.execute(select(Account).where(Account.client_id == client_id))
        accounts = result.scalars().all()
        log_info("BankAPI", f"Запрошены счета клиента {client_id}. Найдено: {len(accounts)}")
        return to_dict_list(accounts)

@router.get("/accounts/{account_id}/tariff")
async def get_tariff(account_id: str):
    """
    Получить информацию о тарифе, привязанном к конкретному счету.
    """
    async with get_db() as db:
        acc = await db.scalar(select(Account).where(Account.id == account_id))
        if not acc: 
            log_error("BankAPI", f"Счет {account_id} не найден при запросе тарифа")
            raise HTTPException(404, "Счет не найден")
            
        tariff = await db.scalar(select(Tariff).where(Tariff.id == acc.tariff_id))
        return to_dict(tariff)

@router.get("/accounts/{account_id}/cards")
async def get_cards(account_id: str):
    """
    Получить список всех карт, привязанных к счету.
    """
    async with get_db() as db:
        result = await db.execute(select(Card).where(Card.account_id == account_id))
        return to_dict_list(result.scalars().all())

@router.get("/accounts/{account_id}/transactions")
async def get_transactions(account_id: str):
    """
    Получить историю транзакций по счету, отсортированную по дате убывания.
    """
    async with get_db() as db:
        result = await db.execute(
            select(Transaction)
            .where(Transaction.account_id == account_id)
            .order_by(Transaction.date.desc())
        )
        return to_dict_list(result.scalars().all())

@router.get("/accounts/{account_id}/autopayments")
async def get_autopayments(account_id: str):
    """
    Получить список активных автоплатежей для счета.
    """
    async with get_db() as db:
        result = await db.execute(select(AutoPayment).where(AutoPayment.account_id == account_id))
        return to_dict_list(result.scalars().all())

@router.post("/accounts/{account_id}/cards/issue")
async def issue_card(account_id: str):
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
        return to_dict(new_card)

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

@router.post("/cards/{card_id}/reissue")
async def reissue_card(card_id: str):
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
        return to_dict(new_card)

@router.post("/accounts/{account_id}/close")
async def close_account(account_id: str):
    """
    Закрыть банковский счет. 
    Требует, чтобы баланс счета был равен нулю.
    """
    async with get_db() as db:
        acc = await db.scalar(select(Account).where(Account.id == account_id))
        if not acc: raise HTTPException(404, "Счет не найден")
        
        if float(acc.balance) > 0:
            log_error("BankAPI", f"Попытка закрыть счет {account_id} с положительным балансом")
            raise HTTPException(400, "Невозможно закрыть счет с положительным балансом")
            
        acc.status = "closed"
        await db.commit()
        log_info("BankAPI", f"Счет {account_id} закрыт")
        return {"status": "success"}

@router.post("/accounts/{account_id}/autopayments")
async def create_autopayment(account_id: str, data: AutoPaymentCreate):
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
        return to_dict(ap)

@router.post("/transfers")
async def transfer_money(data: TransferRequest):
    """
    Универсальный эндпоинт для перевода средств внутри банка.
    Поддерживает переводы по номеру счета, карты или телефона.
    """
    async with get_db() as db:
        sender_acc = await db.scalar(select(Account).where(Account.id == data.from_account_id))
        if not sender_acc: raise HTTPException(404, "Счет отправителя не найден")
        
        if float(sender_acc.balance) < data.amount:
            log_error("BankAPI", f"Недостаточно средств на счете {data.from_account_id} для перевода {data.amount}")
            raise HTTPException(400, "Недостаточно средств")

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

        # Выполнение перевода
        sender_acc.balance = float(sender_acc.balance) - data.amount
        receiver_acc.balance = float(receiver_acc.balance) + data.amount

        # Запись транзакций для истории
        tx_out = Transaction(
            account_id=sender_acc.id,
            category='expense',
            operation_type=f'transfer_out_{data.transfer_type}',
            amount=data.amount,
            description=data.description or f"Перевод получателю: {data.destination}"
        )
        tx_in = Transaction(
            account_id=receiver_acc.id,
            category='income',
            operation_type=f'transfer_in_{data.transfer_type}',
            amount=data.amount,
            description=data.description or f"Перевод от: {sender_acc.account_number}"
        )
        
        db.add(tx_out)
        db.add(tx_in)
        await db.commit()

        log_success("BankAPI", f"Успешный перевод {data.amount} от {sender_acc.id} к {receiver_acc.id}")
        return {"status": "success", "transaction_id": tx_out.id}
