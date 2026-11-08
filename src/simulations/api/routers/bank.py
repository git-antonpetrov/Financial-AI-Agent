from __future__ import annotations

import json
import random
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy.exc import IntegrityError
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from src.simulations.core.bank_claims import remember_bank_movement
from src.simulations.api.core.auth import verify_bank_token
from src.simulations.api.schemas.responses import (
    AccountResponse,
    AutoPaymentResponse,
    BankCertificatesInfoResponse,
    CardResponse,
    TariffResponse,
    TransactionResponse,
)
from src.simulations.core.crypto import get_bank_ca, get_receipt_signer
from src.simulations.core.utils.console_logger import log_error, log_info, log_success
from src.simulations.db.bank.db.client import get_async_session_maker
from src.simulations.db.bank.db.models import (
    Account,
    AutoPayment,
    Card,
    IdempotencyRecord,
    Tariff,
    Transaction,
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


def _receipt_fields(receipt: object) -> dict:
    """Возвращает поля операции из подписанного чека.

    Аргументы:
        receipt: Чек банка. Данные лежат в ``receipt.data``.

    Возвращает:
        Словарь полей. Пустой словарь означает, что чек не содержит данных.
    """
    if not isinstance(receipt, dict):
        return {}
    nested = receipt.get("receipt")
    if isinstance(nested, dict) and isinstance(nested.get("data"), dict):
        return nested["data"]
    data = receipt.get("data")
    if isinstance(data, dict):
        return data
    return {}


def _amounts_equal(left: object, right: object) -> bool:
    """Сравнивает две денежные величины с точностью до копейки.

    Аргументы:
        left: Сохранённая сумма.
        right: Сумма текущего запроса.

    Возвращает:
        True, если обе величины задают одну сумму.
    """
    try:
        return Decimal(str(left)).quantize(Decimal("0.01")) == Decimal(str(right)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return False


def _cached_transfer_matches(record: object, data: TransferRequest) -> bool:
    """Проверяет, что кэш перевода описывает тот же счёт, назначение и сумму.

    Аргументы:
        record: Запись идемпотентности перевода.
        data: Текущий запрос перевода.

    Возвращает:
        True, если ключ занят этим же переводом.
    """
    if str(getattr(record, "from_account_id", "")) != str(data.from_account_id):
        return False
    if not _amounts_equal(getattr(record, "amount", None), data.amount):
        return False
    if data.to_account_id and str(getattr(record, "to_account_id", "")) != str(data.to_account_id):
        return False
    raw = getattr(record, "response_payload", None)
    if not isinstance(raw, str) or not raw.strip():
        return data.to_account_id is not None
    parsed = json.loads(raw)
    receipt = parsed.get("signed_receipt") if isinstance(parsed, dict) else None
    receipt_data = _receipt_fields(receipt)
    if not receipt_data:
        return data.to_account_id is not None
    if str(receipt_data.get("from_account_id", "")) != str(data.from_account_id):
        return False
    if not _amounts_equal(receipt_data.get("amount"), data.amount):
        return False
    if str(receipt_data.get("destination", "")) != str(data.destination):
        return False
    if str(receipt_data.get("transfer_type", "")) != str(data.transfer_type):
        return False
    if data.to_account_id and str(receipt_data.get("to_account_id", "")) != str(data.to_account_id):
        return False
    return True


def get_db():
    """Возвращает асинхронную сессию банковской базы."""
    session_maker = get_async_session_maker()
    return session_maker()


# --- Схемы данных (Pydantic) ---


class TransferRequest(BaseModel):
    """
    Схема запроса для перевода средств внутри банка.
    """

    from_account_id: str
    to_account_id: str | None = None
    amount: Decimal = Field(..., gt=Decimal("0.00"), description="Сумма перевода (строго больше нуля)")
    transfer_type: str  # Типы: 'account', 'phone', 'card'
    destination: str  # Номер счета, номер телефона или номер карты
    description: str | None = None
    idempotency_key: str | None = None



class AutoPaymentCreate(BaseModel):
    """
    Схема запроса для создания нового автоплатежа.
    """

    amount: Decimal = Field(..., gt=Decimal("0.00"), description="Сумма автоплатежа (строго больше нуля)")
    recipient: str
    schedule: str  # Расписание: 'monthly', 'weekly'
    next_payment_date: date
    idempotency_key: str | None = None


# --- Эндпоинты ---


@router.get("/accounts/by-client/{client_id}", response_model=list[AccountResponse])
async def get_accounts(client_id: str) -> list[AccountResponse]:
    """Возвращает счета указанного клиента."""
    async with get_db() as db:
        result = await db.execute(select(Account).where(Account.client_id == client_id))
        accounts = result.scalars().all()
        log_info("BankAPI", f"Запрошены счета клиента {client_id}. Найдено: {len(accounts)}")
        return [AccountResponse.model_validate(acc) for acc in accounts]


@router.get("/accounts/{account_id}/tariff", response_model=TariffResponse)
async def get_tariff(account_id: str) -> TariffResponse:
    """Возвращает тариф, привязанный к счёту."""
    async with get_db() as db:
        acc = await db.scalar(select(Account).where(Account.id == account_id))
        if not acc:
            log_error("BankAPI", f"Счет {account_id} не найден при запросе тарифа")
            raise HTTPException(404, "Счет не найден")

        tariff = await db.scalar(select(Tariff).where(Tariff.id == acc.tariff_id))
        return TariffResponse.model_validate(tariff)


@router.get("/accounts/{account_id}/cards", response_model=list[CardResponse])
async def get_cards(account_id: str) -> list[CardResponse]:
    """Возвращает карты, привязанные к счёту."""
    async with get_db() as db:
        result = await db.execute(select(Card).where(Card.account_id == account_id))
        return [CardResponse.model_validate(c) for c in result.scalars().all()]


@router.get("/accounts/{account_id}/transactions", response_model=list[TransactionResponse])
async def get_transactions(account_id: str) -> list[TransactionResponse]:
    """Возвращает транзакции счёта от новых к старым."""
    async with get_db() as db:
        result = await db.execute(
            select(Transaction).where(Transaction.account_id == account_id).order_by(Transaction.date.desc())
        )
        return [TransactionResponse.model_validate(t) for t in result.scalars().all()]


@router.get("/accounts/{account_id}/autopayments", response_model=list[AutoPaymentResponse])
async def get_autopayments(account_id: str) -> list[AutoPaymentResponse]:
    """Возвращает активные автоплатежи счёта."""
    async with get_db() as db:
        result = await db.execute(select(AutoPayment).where(AutoPayment.account_id == account_id))
        return [AutoPaymentResponse.model_validate(ap) for ap in result.scalars().all()]


def mask_card_number(card: str) -> str:
    """Маскирует номер банковской карты по стандарту PCI DSS."""
    card_str = str(card)
    if len(card_str) >= 8:
        return f"{card_str[:4]}********{card_str[-4:]}"
    return "****"


async def generate_unique_card_number(db: AsyncSession, max_attempts: int = 10) -> str:
    """Генерирует уникальный 16-значный номер карты с валидацией коллизий в базе данных."""
    for _ in range(max_attempts):
        candidate = f"4276{random.randint(100000000000, 999999999999)}"
        exists = await db.scalar(select(Card.id).where(Card.card_number == candidate))
        if not exists:
            return candidate
    raise HTTPException(status_code=500, detail="Не удалось сгенерировать уникальный номер карты")


@router.post("/accounts/{account_id}/cards/issue", response_model=CardResponse)
async def issue_card(account_id: str) -> CardResponse:
    """
    Выпустить новую карту и привязать ее к существующему счету.
    """
    async with get_db() as db:
        acc = await db.scalar(select(Account).where(Account.id == account_id))
        if not acc:
            log_error(
                "BankAPI",
                f"Попытка выпустить карту для несуществующего счета {account_id}",
            )
            raise HTTPException(404, "Счет не найден")

        # Генерирует уникальный номер карты с проверкой коллизий
        card_number = await generate_unique_card_number(db)
        new_card = Card(account_id=account_id, card_number=card_number)
        db.add(new_card)
        await db.commit()
        await db.refresh(new_card)
        log_success(
            "BankAPI",
            f"Успешно выпущена карта {mask_card_number(new_card.card_number)} для счета {account_id}",
        )
        return CardResponse.model_validate(new_card)


@router.post("/cards/{card_id}/block")
async def block_card(card_id: str):
    """
    Заблокировать карту (необратимое действие в рамках симуляции,
    для разблокировки нужен перевыпуск).
    """
    async with get_db() as db:
        card = await db.scalar(select(Card).where(Card.id == card_id))
        if not card:
            raise HTTPException(404, "Карта не найдена")

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
        if not card:
            raise HTTPException(404, "Карта не найдена")

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
        if not card:
            raise HTTPException(404, "Карта не найдена")

        card.status = "blocked"

        card_number = await generate_unique_card_number(db)
        new_card = Card(account_id=card.account_id, card_number=card_number)
        db.add(new_card)
        await db.commit()
        await db.refresh(new_card)
        log_success(
            "BankAPI",
            f"Карта {card_id} перевыпущена. Новый номер: {mask_card_number(new_card.card_number)}",
        )
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
        acc = await db.scalar(select(Account).where(Account.id == account_id).with_for_update())
        if not acc:
            raise HTTPException(404, "Счет не найден")

        if acc.status == "closed":
            raise HTTPException(400, "Счет уже закрыт")

        if Decimal(str(acc.balance)) != Decimal("0.00"):
            log_error(
                "BankAPI",
                f"Попытка закрыть счет {account_id} с ненулевым балансом ({acc.balance})",
            )
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
            if receipt.get("cert_fingerprint"):
                response.headers["X-Bank-Cert-Fingerprint"] = receipt["cert_fingerprint"]
            if receipt.get("bank_cert_serial"):
                response.headers["X-Bank-Cert-Serial"] = str(receipt["bank_cert_serial"])
        return {"status": "success", "signed_receipt": receipt}


def _require_autopayment_key(request: Request | None, explicit: str | None) -> str:
    """Возвращает ключ идемпотентности создания автоплатежа.

    Аргументы:
        request: HTTP-запрос. В модульном тесте может отсутствовать.
        explicit: Ключ из тела запроса, если заголовка нет.

    Возвращает:
        Непустой ключ.

    Вызывает:
        HTTPException: Ключ не передан.
    """
    header = None
    if request is not None:
        raw_header = request.headers.get("X-Idempotency-Key")
        if isinstance(raw_header, str) and raw_header.strip():
            header = raw_header.strip()
    body = explicit.strip() if isinstance(explicit, str) and explicit.strip() else None
    key = header or body
    if not key:
        raise HTTPException(400, "Требуется заголовок X-Idempotency-Key")
    return key


def _same_autopayment(existing: AutoPayment, account_id: str, data: AutoPaymentCreate) -> bool:
    """Сравнивает сохранённый автоплатёж с повторным запросом.

    Аргументы:
        existing: Уже записанный автоплатёж.
        account_id: Счёт текущего запроса.
        data: Тело повторного запроса.

    Возвращает:
        True, если счёт, сумма, получатель и расписание совпадают.
    """
    return (
        str(existing.account_id) == str(account_id)
        and Decimal(str(existing.amount)) == data.amount
        and str(existing.recipient) == data.recipient
        and str(existing.schedule) == data.schedule
        and existing.next_payment_date == data.next_payment_date
    )


async def _load_autopayment(db, key: str) -> AutoPayment | None:
    """Загружает автоплатёж с этим ключом идемпотентности.

    Аргументы:
        db: Банковская сессия.
        key: Ключ идемпотентности.

    Возвращает:
        Строку автоплатежа либо None, если ключ принадлежит другой сущности.
    """
    existing = await db.scalar(select(AutoPayment).where(AutoPayment.idempotency_key == key))
    if getattr(existing, "idempotency_key", None) != key:
        return None
    return existing


@router.post("/accounts/{account_id}/autopayments", response_model=AutoPaymentResponse)
async def create_autopayment(
    account_id: str,
    data: AutoPaymentCreate,
    request: Request = None,
) -> AutoPaymentResponse:
    """Создаёт автоплатёж один раз для ключа идемпотентности.

    Повтор с тем же ключом возвращает первую строку и не создаёт вторую,
    поэтому закрытие дня не списывает одну и ту же инструкцию дважды.

    Аргументы:
        account_id: Счёт, с которого списывается автоплатёж.
        data: Сумма, получатель, расписание и ключ идемпотентности.
        request: HTTP-запрос. В модульном тесте может отсутствовать.

    Возвращает:
        Созданный или ранее сохранённый автоплатёж.

    Вызывает:
        HTTPException: Нет ключа или ключ уже использован для другого автоплатежа.

    Побочные эффекты:
        Добавляет одну строку автоплатежа.
    """
    key = _require_autopayment_key(request, data.idempotency_key)
    async with get_db() as db:
        existing = await _load_autopayment(db, key)
        if existing is not None:
            if not _same_autopayment(existing, account_id, data):
                raise HTTPException(409, "Ключ идемпотентности уже использован другой операцией")
            return AutoPaymentResponse.model_validate(existing)
        ap = AutoPayment(
            account_id=account_id,
            amount=data.amount,
            recipient=data.recipient,
            schedule=data.schedule,
            next_payment_date=data.next_payment_date,
            idempotency_key=key,
        )
        db.add(ap)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raced = await _load_autopayment(db, key)
            if raced is not None and _same_autopayment(raced, account_id, data):
                return AutoPaymentResponse.model_validate(raced)
            raise HTTPException(409, "Ключ идемпотентности уже использован другой операцией") from None
        await db.refresh(ap)
        log_success(
            "BankAPI",
            f"Создан автоплатеж для счета {account_id} на сумму {data.amount}",
        )
        return AutoPaymentResponse.model_validate(ap)


@router.post("/transfers")
async def transfer_money(
    data: TransferRequest,
    request: Request = None,
    response: Response = None,
    x_idempotency_key: str | None = Header(None, alias="X-Idempotency-Key"),
):
    """
    Универсальный эндпоинт для перевода средств внутри банка.
    Поддерживает переводы по номеру счета, карты или телефона.
    Реализует строгую идемпотентность через заголовок X-Idempotency-Key или поле idempotency_key.
    Использует пессимистическую блокировку строк (SELECT FOR UPDATE)
    в детерминированном порядке ID для защиты от состояния гонки и взаимных блокировок.
    Проверка и валидация счета получателя выполняются безопасно внутри заблокированной транзакции.
    Подписывает сформированный чек-ответ приватным ключом Банка (Блок 5 схемы arch.txt).
    """
    header_key = None
    if request is not None:
        raw_header = request.headers.get("X-Idempotency-Key")
        if isinstance(raw_header, str) and raw_header.strip():
            header_key = raw_header.strip()
    elif isinstance(x_idempotency_key, str) and x_idempotency_key.strip():
        header_key = x_idempotency_key.strip()
    body_key = str(data.idempotency_key).strip() if data.idempotency_key else ""
    effective_idempotency_key = header_key or body_key or None
    if not effective_idempotency_key:
        raise HTTPException(400, "Требуется заголовок X-Idempotency-Key")

    async with get_db() as db:
        # 1. Проверка идемпотентности: возврат кэшированного ответа без повторного списания
        if effective_idempotency_key:
            existing_rec = await db.scalar(
                select(IdempotencyRecord).where(IdempotencyRecord.key == effective_idempotency_key)
            )
            if (
                getattr(existing_rec, "key", None) == effective_idempotency_key
                and _cached_transfer_matches(existing_rec, data)
                and getattr(existing_rec, "response_payload", None)
            ):
                log_info(
                    "BankAPI",
                    f"Идемпотентный повтор запроса: кэш для ключа {effective_idempotency_key}",
                )
                cached_data = json.loads(existing_rec.response_payload)
                if response is not None and "signed_receipt" in cached_data:
                    receipt = cached_data["signed_receipt"]
                    if isinstance(receipt, dict):
                        if "signature" in receipt:
                            response.headers["X-Bank-Signature"] = receipt["signature"]
                        if receipt.get("cert_fingerprint"):
                            response.headers["X-Bank-Cert-Fingerprint"] = receipt["cert_fingerprint"]
                        if receipt.get("bank_cert_serial"):
                            response.headers["X-Bank-Cert-Serial"] = str(receipt["bank_cert_serial"])
                return cached_data

            if (
                getattr(existing_rec, "key", None) == effective_idempotency_key
                and not _cached_transfer_matches(existing_rec, data)
            ):
                raise HTTPException(409, "Ключ идемпотентности уже использован другой операцией")

            existing_tx = await db.scalar(
                select(Transaction).where(
                    Transaction.idempotency_key == effective_idempotency_key,
                    Transaction.category == "expense",
                )
            )
            if getattr(existing_tx, "idempotency_key", None) == effective_idempotency_key:
                raise HTTPException(409, "Ключ идемпотентности уже использован другой операцией")

        receiver_acc = None

        # Определение целевого счета для взаимной блокировки
        if data.to_account_id:
            receiver_acc = await db.scalar(select(Account).where(Account.id == data.to_account_id))
        elif data.transfer_type == "account":
            receiver_acc = await db.scalar(select(Account).where(Account.account_number == data.destination))
        elif data.transfer_type == "card":
            card = await db.scalar(select(Card).where(Card.card_number == data.destination))
            if card:
                receiver_acc = await db.scalar(select(Account).where(Account.id == card.account_id))
        elif data.transfer_type == "phone":
            receiver_acc = await db.scalar(
                select(Account)
                .where(
                    Account.client_id == data.destination,
                    Account.status == "active",
                    Account.currency == "RUB",
                )
                .order_by(Account.created_at.asc())
            )
            if not receiver_acc:
                client_exists = await db.scalar(select(Account.id).where(Account.client_id == data.destination))
                if client_exists:
                    log_error(
                        "BankAPI",
                        f"У клиента {data.destination} нет активных рублевых счетов для зачисления",
                    )
                    raise HTTPException(
                        400,
                        "У получателя отсутствуют активные рублевые счета для зачисления перевода",
                    )

        if not receiver_acc:
            log_error(
                "BankAPI",
                f"Получатель не найден. Тип: {data.transfer_type}, Назначение: {data.destination}",
            )
            raise HTTPException(404, "Получатель не найден")

        if str(data.from_account_id) == str(receiver_acc.id):
            log_error("BankAPI", f"Попытка самоперевода на счет {data.from_account_id}")
            raise HTTPException(400, "Перевод на тот же самый счет невозможен")

        # Детерминированный порядок блокировки для предотвращения взаимных блокировок (deadlocks)
        lock_ids = sorted([str(data.from_account_id), str(receiver_acc.id)])
        accounts_res = await db.execute(
            select(Account).where(Account.id.in_(lock_ids)).order_by(Account.id).with_for_update()
        )
        accounts_map = {str(acc.id): acc for acc in accounts_res.scalars().all()}

        sender = accounts_map.get(str(data.from_account_id))
        if not sender:
            raise HTTPException(404, "Счет отправителя не найден")

        receiver = accounts_map.get(str(receiver_acc.id))
        if not receiver:
            raise HTTPException(404, "Счет получателя не найден")

        # Повторная валидация состояния обоих счетов строго внутри атомарной блокировки (with_for_update)
        if str(sender.id) == str(receiver.id):
            raise HTTPException(400, "Перевод на тот же самый счет невозможен")

        if sender.status and sender.status != "active":
            raise HTTPException(
                400,
                f"Счет отправителя недоступен для перевода (статус: {sender.status})",
            )

        if receiver.status and receiver.status != "active":
            raise HTTPException(
                400,
                f"Счет получателя недоступен для перевода (статус: {receiver.status})",
            )

        # Валидация неизменности реквизитов получателя под блокировкой
        if data.transfer_type == "phone":
            if receiver.currency != "RUB" or receiver.status != "active":
                raise HTTPException(
                    400,
                    "У получателя отсутствуют активные рублевые счета для зачисления перевода",
                )

        if sender.currency != receiver.currency:
            raise HTTPException(
                400,
                f"Межвалютные переводы без конвертации не поддерживаются "
                f"(отправитель: {sender.currency}, получатель: {receiver.currency})",
            )

        sender_balance = Decimal(str(sender.balance))
        if sender_balance < data.amount:
            log_error(
                "BankAPI",
                f"Недостаточно средств на счете {sender.id} для перевода {data.amount}",
            )
            raise HTTPException(400, "Недостаточно средств")

        # Выполнение перевода
        sender.balance = sender_balance - data.amount
        receiver.balance = Decimal(str(receiver.balance)) + data.amount

        # Запись транзакций для истории с фиксацией ключа идемпотентности
        tx_out = Transaction(
            account_id=sender.id,
            category="expense",
            operation_type=f"transfer_out_{data.transfer_type}",
            amount=data.amount,
            description=data.description or f"Перевод получателю: {data.destination}",
            idempotency_key=effective_idempotency_key,
        )
        tx_in = Transaction(
            account_id=receiver.id,
            category="income",
            operation_type=f"transfer_in_{data.transfer_type}",
            amount=data.amount,
            description=data.description or f"Перевод от: {sender.account_number}",
            idempotency_key=effective_idempotency_key,
        )

        db.add(tx_out)
        db.add(tx_in)
        remember_bank_movement(
            db,
            key=effective_idempotency_key,
            operation_type="bank_transfer",
            account_id=str(sender.id),
            amount=data.amount,
            direction="expense",
        )

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

        result_payload = {
            "status": "success",
            "transaction_id": tx_out.id,
            "signed_receipt": receipt,
        }

        # Сохранение записи идемпотентности в выделенную таблицу
        if effective_idempotency_key:
            idemp_rec = IdempotencyRecord(
                key=effective_idempotency_key,
                from_account_id=sender.id,
                to_account_id=receiver.id,
                amount=data.amount,
                transaction_id=tx_out.id,
                response_payload=json.dumps(result_payload),
            )
            db.add(idemp_rec)

        await db.commit()

        log_success("BankAPI", f"Успешный перевод {data.amount} от {sender.id} к {receiver.id}")

        if response is not None:
            response.headers["X-Bank-Signature"] = receipt["signature"]
            if receipt.get("cert_fingerprint"):
                response.headers["X-Bank-Cert-Fingerprint"] = receipt["cert_fingerprint"]
            if receipt.get("bank_cert_serial"):
                response.headers["X-Bank-Cert-Serial"] = str(receipt["bank_cert_serial"])

        return result_payload

