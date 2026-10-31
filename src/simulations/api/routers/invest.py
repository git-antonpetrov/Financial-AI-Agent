import json
import random
import uuid
from datetime import datetime
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.future import select

from src.simulations.api.bank_claims import (
    begin_bank_movement,
    canonical_amount,
    release_bank_claim,
    remember_bank_movement,
)
from src.simulations.api.core.auth import verify_invest_token
from src.simulations.api.schemas.responses import (
    BrokerAccountResponse,
    DepositResponse,
    InvestmentStrategyResponse,
    PortfolioResponse,
    SavingsAccountResponse,
)
from src.simulations.core.crypto import get_receipt_signer
from src.simulations.core.utils.console_logger import log_error, log_info, log_success
from src.simulations.db.bank.db.client import (
    get_async_session_maker as get_bank_session,
)
from src.simulations.db.bank.db.models import Account, Transaction
from src.simulations.db.invest.db.client import (
    get_async_session_maker as get_invest_session,
)
from src.simulations.db.invest.db.models import (
    BrokerAccount,
    Deposit,
    InvestmentStrategy,
    SavingsAccount,
)

router = APIRouter(dependencies=[Depends(verify_invest_token)])


def invest_db():
    """Фабрика для получения сессии инвестиционной БД."""
    return get_invest_session()()


def bank_db():
    """Фабрика для получения сессии банковской БД."""
    return get_bank_session()()


def require_idempotency_key(request: Request | None, explicit: str | None) -> str:
    """Возвращает ключ идемпотентности из заголовка или тела запроса.

    Аргументы:
        request: HTTP-запрос. Может отсутствовать в модульном тесте.
        explicit: Значение поля idempotency_key, если заголовка нет.

    Возвращает:
        Непустой ключ идемпотентности.

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


def signed_response(
    row: object,
    key: str,
    key_attr: str = "idempotency_key",
    payload_attr: str = "response_payload",
) -> dict | None:
    """Возвращает сохранённый подписанный ответ, если ключ совпадает.

    Аргументы:
        row: Строка продукта или транзакции. Пустое значение означает, что повтора нет.
        key: Ключ идемпотентности текущего запроса.
        key_attr: Имя поля ключа.
        payload_attr: Имя поля с JSON ответа.

    Возвращает:
        Словарь с полем signed_receipt либо None, если сохранённого ответа нет.

    Вызывает:
        ValueError: Сохранённый JSON не является подписанным ответом.
    """
    if getattr(row, key_attr, None) != key:
        return None
    raw = getattr(row, payload_attr, None)
    if not isinstance(raw, str) or not raw.strip():
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("signed_receipt"), dict):
        raise ValueError("Сохранённый ответ не содержит подписанный чек")
    return parsed


def apply_receipt_headers(response: Response | None, receipt: dict) -> None:
    """Копирует подпись сохранённого чека в заголовки ответа.

    Аргументы:
        response: HTTP-ответ. В модульном тесте может отсутствовать.
        receipt: Подписанный чек первого выполнения.
    """
    if response is None:
        return
    signature = receipt.get("signature")
    if isinstance(signature, str) and signature:
        response.headers["X-Bank-Signature"] = signature
    receipt_body = receipt.get("receipt")
    if isinstance(receipt_body, dict):
        receipt_id = receipt_body.get("receipt_id")
        if receipt_id:
            response.headers["X-Receipt-ID"] = str(receipt_id)


async def store_product_response(model_cls: type, row_id: str, key: str, payload: dict) -> None:
    """Записывает подписанный ответ в строку продукта.

    Аргументы:
        model_cls: Модель копилки или брокерского счёта.
        row_id: Идентификатор строки, снятый до закрытия предыдущей сессии.
        key: Ключ идемпотентности.
        payload: Ответ с полем signed_receipt.

    Побочные эффекты:
        Обновляет response_payload. Пропускает запись, если повторная загрузка не находит эту строку.
    """
    raw = json.dumps(payload, ensure_ascii=False)
    async with invest_db() as idb:
        row = await idb.scalar(select(model_cls).where(model_cls.id == row_id))
        if getattr(row, "idempotency_key", None) != key:
            return
        row.response_payload = raw
        await idb.commit()


async def assert_bank_funds(account_id: str, client_id: str, amount: Decimal, currency_error: str) -> None:
    """Проверяет банковский счёт и не фиксирует списание.

    Аргументы:
        account_id: Идентификатор счёта списания.
        client_id: Клиент, которому должен принадлежать счёт.
        amount: Сумма будущей проводки.
        currency_error: Текст отказа, если валюта счёта не RUB.

    Вызывает:
        HTTPException: Счёт не найден, чужой, неактивен или средств недостаточно.
    """
    async with bank_db() as bdb:
        acc = await bdb.scalar(select(Account).where(Account.id == account_id).with_for_update())
        if not acc:
            raise HTTPException(404, "Банковский счет не найден")
        if acc.client_id and client_id and str(acc.client_id) != str(client_id):
            raise HTTPException(403, "Банковский счет списания не принадлежит указанному клиенту")
        if acc.status and acc.status != "active":
            raise HTTPException(400, f"Банковский счет списания недоступен (статус: {acc.status})")
        if acc.currency and acc.currency != "RUB":
            raise HTTPException(400, currency_error)
        if Decimal(str(acc.balance)) < amount:
            raise HTTPException(400, "Недостаточно средств на банковском счете")


async def debit_bank_once(
    account_id: str,
    amount: Decimal,
    key: str,
    operation_type: str,
    description: str,
) -> None:
    """Списывает сумму один раз для ключа идемпотентности.

    Повторный вызов с тем же ключом не меняет баланс.

    Аргументы:
        account_id: Счёт списания.
        amount: Сумма списания.
        key: Ключ идемпотентности проводки.
        operation_type: Тип банковской операции.
        description: Назначение проводки.

    Вызывает:
        HTTPException: Счёт исчез или средств уже недостаточно.
    """
    async with bank_db() as bdb:
        should_post = await begin_bank_movement(
            bdb,
            key=key,
            operation_type=operation_type,
            account_id=account_id,
            amount=amount,
            direction="expense",
        )
        if not should_post:
            return
        acc = await bdb.scalar(select(Account).where(Account.id == account_id).with_for_update())
        if not acc:
            raise HTTPException(404, "Банковский счет не найден")
        balance = Decimal(str(acc.balance))
        if balance < amount:
            raise HTTPException(400, "Недостаточно средств на банковском счете")
        acc.balance = balance - amount
        bdb.add(
            Transaction(
                account_id=acc.id,
                category="expense",
                operation_type=operation_type,
                amount=amount,
                description=description,
                idempotency_key=key,
            )
        )
        remember_bank_movement(
            bdb,
            key=key,
            operation_type=operation_type,
            account_id=str(acc.id),
            amount=amount,
            direction="expense",
        )
        await bdb.commit()


async def _remove_invest_row(model_cls: type, row_id: str | None) -> None:
    """Удаляет актив, если списание банка после его создания не состоялось.

    Баланс банка не увеличивает: списание либо не записано, либо его откатывает сессия банка.

    Аргументы:
        model_cls: Модель вклада, копилки или брокерского счёта.
        row_id: Идентификатор созданной строки.

    Вызывает:
        RuntimeError: Строка не найдена или удаление не записано.
    """
    if not row_id:
        raise RuntimeError("Компенсация актива невозможна: идентификатор пуст")
    try:
        async with invest_db() as idb:
            row = await idb.scalar(select(model_cls).where(model_cls.id == row_id))
            if row is None:
                raise RuntimeError("Компенсация актива не выполнена: запись не найдена")
            await idb.delete(row)
            await idb.commit()
    except Exception as exc:
        log_error("InvestAPI", f"Не удалось удалить актив {row_id}: {exc}")
        raise


async def generate_unique_invest_account_number(db, model_cls: type, prefix: str, max_attempts: int = 10) -> str:
    """Генерирует уникальный 20-значный номер счета с проверкой коллизий в базе данных."""
    for _ in range(max_attempts):
        candidate = f"{prefix}{random.randint(1000000000000000, 9999999999999999)}"
        exists = await db.scalar(select(model_cls.id).where(model_cls.account_number == candidate))
        if not exists:
            return candidate
    raise HTTPException(
        status_code=500,
        detail=f"Не удалось сгенерировать уникальный номер счета {model_cls.__name__}",
    )


# --- Схемы данных (Pydantic) ---


class OpenProductRequest(BaseModel):
    """Схема запроса для открытия инвестиционного продукта (вклад, копилка, брокерский счет)."""

    client_id: str
    from_bank_account_id: str
    initial_amount: Decimal = Field(..., gt=Decimal("0.00"), description="Начальная сумма (строго больше нуля)")
    term_months: int | None = Field(None, gt=0, description="Срок вклада в месяцах (строго больше нуля)")
    idempotency_key: str | None = None


class StrategySubscription(BaseModel):
    """Схема для управления подпиской на инвестиционную стратегию."""

    strategy_id: str | None = None


class CloseProductRequest(BaseModel):
    """Схема для закрытия продукта."""

    to_bank_account_id: str
    idempotency_key: str | None = None


# --- Эндпоинты ---


@router.get("/strategies", response_model=list[InvestmentStrategyResponse])
async def get_strategies() -> list[InvestmentStrategyResponse]:
    """
    Получить список всех доступных инвестиционных стратегий.
    """
    async with invest_db() as db:
        result = await db.execute(select(InvestmentStrategy))
        return [InvestmentStrategyResponse.model_validate(s) for s in result.scalars().all()]


# Базовые параметры тарифной сетки финансовых продуктов
PRODUCT_CONDITIONS = {
    "key_rate": Decimal("15.00"),
    "deposit_rates": {
        "6_months": Decimal("15.00"),
        "12_months": Decimal("15.00"),
    },
    "default_deposit_rate": Decimal("15.00"),
    "savings_account_rate": Decimal("10.00"),
}


def get_product_terms() -> dict:
    """Возвращает актуальные тарифные условия банка по депозитам и накопительным счетам."""
    return {
        "key_rate": PRODUCT_CONDITIONS["key_rate"],
        "deposit_rates": {k: v for k, v in PRODUCT_CONDITIONS["deposit_rates"].items()},
        "savings_account_rate": PRODUCT_CONDITIONS["savings_account_rate"],
    }


def calculate_deposit_rate(term_months: int | None) -> Decimal:
    """Рассчитывает процентную ставку по вкладу в зависимости от выбранного срока."""
    if term_months == 6 and "6_months" in PRODUCT_CONDITIONS["deposit_rates"]:
        return PRODUCT_CONDITIONS["deposit_rates"]["6_months"]
    if term_months == 12 and "12_months" in PRODUCT_CONDITIONS["deposit_rates"]:
        return PRODUCT_CONDITIONS["deposit_rates"]["12_months"]
    return PRODUCT_CONDITIONS["default_deposit_rate"]


def calculate_savings_rate() -> Decimal:
    """Возвращает актуальную процентную ставку для накопительного счета."""
    return PRODUCT_CONDITIONS["savings_account_rate"]


@router.get("/products/terms")
async def get_terms():
    """
    Получить текущие условия по продуктам (ставки, сроки).
    """
    return get_product_terms()


@router.get("/portfolio/{client_id}", response_model=PortfolioResponse)
async def get_portfolio(client_id: str) -> PortfolioResponse:
    """
    Получить сводный инвестиционный портфель клиента (копилки, вклады, брокерские счета).
    """
    async with invest_db() as db:
        savings = (
            (await db.execute(select(SavingsAccount).where(SavingsAccount.client_id == client_id))).scalars().all()
        )
        deposits = (await db.execute(select(Deposit).where(Deposit.client_id == client_id))).scalars().all()
        brokers = (await db.execute(select(BrokerAccount).where(BrokerAccount.client_id == client_id))).scalars().all()

        log_info("InvestAPI", f"Собран портфель для клиента {client_id}")
        return PortfolioResponse(
            savings_accounts=[SavingsAccountResponse.model_validate(s) for s in savings],
            deposits=[DepositResponse.model_validate(d) for d in deposits],
            broker_accounts=[BrokerAccountResponse.model_validate(b) for b in brokers],
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

    await assert_bank_funds(
        req.from_bank_account_id,
        req.client_id,
        req.initial_amount,
        "Открытие вклада возможно только со счета в валюте RUB",
    )
    idem_key = require_idempotency_key(request, req.idempotency_key)

    deposit_id = None
    try:
        async with invest_db() as idb:
            existing = await idb.scalar(select(Deposit).where(Deposit.idempotency_key == idem_key))
            if getattr(existing, "idempotency_key", None) == idem_key:
                dep = existing
            else:
                interest_rate = calculate_deposit_rate(req.term_months)
                monthly_rate = interest_rate / Decimal(100) / Decimal(12)
                first_payment = (req.initial_amount * monthly_rate).quantize(Decimal("0.01"))
                next_pay_date = datetime.now() + relativedelta(months=1)
                account_number = await generate_unique_invest_account_number(idb, Deposit, "4230")
                dep = Deposit(
                    client_id=req.client_id,
                    account_number=account_number,
                    balance=req.initial_amount,
                    interest_rate=interest_rate,
                    term_months=req.term_months,
                    next_payment_date=next_pay_date,
                    next_payment_amount=first_payment,
                    idempotency_key=idem_key,
                )
                idb.add(dep)
                await idb.commit()
                await idb.refresh(dep)
            deposit_id = str(dep.id)
            result = DepositResponse.model_validate(dep)
            log_success(
                "InvestAPI",
                f"Вклад {dep.account_number} подготовлен для клиента {req.client_id}",
            )
    except Exception as e:
        log_error("InvestAPI", f"Ошибка при открытии вклада до списания банка: {e}")
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(500, f"Ошибка создания вклада: {e}") from e

    try:
        await debit_bank_once(
            req.from_bank_account_id,
            req.initial_amount,
            idem_key,
            "transfer_out_invest",
            "Открытие вклада",
        )
    except Exception as e:
        await _remove_invest_row(Deposit, deposit_id)
        log_error("InvestAPI", f"Списание за вклад не выполнено, актив удалён: {e}")
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(500, f"Списание за вклад не выполнено, актив отменён: {e}") from e

    request_nonce = None
    if request is not None:
        request_nonce = getattr(request.state, "enclave_nonce", None) or request.headers.get("X-Nonce")
    receipt = get_receipt_signer().create_and_sign_receipt(
        action="invest_deposit_open",
        data={
            "client_id": req.client_id,
            "amount": str(req.initial_amount),
            "term_months": req.term_months,
            "deposit_id": deposit_id,
            "account_number": result.account_number,
        },
        request_nonce=request_nonce,
    )
    if response is not None:
        response.headers["X-Bank-Signature"] = receipt["signature"]
        response.headers["X-Receipt-ID"] = receipt["receipt"]["receipt_id"]
    return result


@router.post("/savings/open", response_model=SavingsAccountResponse)
async def open_savings(
    req: OpenProductRequest,
    request: Request = None,
    response: Response = None,
) -> SavingsAccountResponse:
    """Открывает накопительный счёт после проверки банка и до списания.

    Повтор с тем же ключом возвращает подписанный ответ первого выполнения
    и не создаёт второе списание.
    """
    await assert_bank_funds(
        req.from_bank_account_id,
        req.client_id,
        req.initial_amount,
        "Открытие накопительного счета возможно только со счета в валюте RUB",
    )
    idem_key = require_idempotency_key(request, req.idempotency_key)
    savings_id = None
    created_now = False
    try:
        async with invest_db() as idb:
            existing = await idb.scalar(select(SavingsAccount).where(SavingsAccount.idempotency_key == idem_key))
            stored = signed_response(existing, idem_key)
            if stored is not None:
                apply_receipt_headers(response, stored["signed_receipt"])
                return SavingsAccountResponse.model_validate(stored["body"])
            if getattr(existing, "idempotency_key", None) == idem_key:
                sav = existing
            else:
                interest_rate = calculate_savings_rate()
                monthly_rate = interest_rate / Decimal(100) / Decimal(12)
                first_payment = (req.initial_amount * monthly_rate).quantize(Decimal("0.01"))
                next_pay_date = datetime.now() + relativedelta(months=1)
                account_number = await generate_unique_invest_account_number(idb, SavingsAccount, "4081")
                sav = SavingsAccount(
                    client_id=req.client_id,
                    account_number=account_number,
                    balance=req.initial_amount,
                    interest_rate=interest_rate,
                    next_payment_date=next_pay_date,
                    next_payment_amount=first_payment,
                    idempotency_key=idem_key,
                )
                if sav.id is None:
                    sav.id = str(uuid.uuid4())
                idb.add(sav)
                await idb.commit()
                await idb.refresh(sav)
                created_now = True
            savings_id = str(sav.id)
            result = SavingsAccountResponse.model_validate(sav)
    except Exception as exc:
        log_error("InvestAPI", f"Ошибка создания накопительного счёта до списания: {exc}")
        if isinstance(exc, HTTPException):
            raise exc
        raise HTTPException(500, f"Ошибка создания накопительного счета: {exc}") from exc

    try:
        await debit_bank_once(
            req.from_bank_account_id,
            req.initial_amount,
            idem_key,
            "transfer_out_savings",
            "Открытие накопительного счета",
        )
    except Exception as exc:
        if created_now:
            await _remove_invest_row(SavingsAccount, savings_id)
        if isinstance(exc, HTTPException):
            raise exc
        raise HTTPException(500, f"Списание за копилку не выполнено, актив отменён: {exc}") from exc

    request_nonce = None
    if request is not None:
        request_nonce = getattr(request.state, "enclave_nonce", None) or request.headers.get("X-Nonce")
    receipt = get_receipt_signer().create_and_sign_receipt(
        action="invest_savings_open",
        data={
            "client_id": req.client_id,
            "amount": str(req.initial_amount),
            "savings_id": savings_id,
            "account_number": result.account_number,
        },
        request_nonce=request_nonce,
    )
    payload = {"body": result.model_dump(mode="json"), "signed_receipt": receipt}
    await store_product_response(SavingsAccount, savings_id, idem_key, payload)
    apply_receipt_headers(response, receipt)
    return result


@router.post("/broker-accounts/open", response_model=BrokerAccountResponse)
async def open_broker_account(
    req: OpenProductRequest,
    request: Request = None,
    response: Response = None,
) -> BrokerAccountResponse:
    """Открывает брокерский счёт до списания в банке.

    Повтор с тем же ключом возвращает подписанный ответ первого выполнения
    и не списывает сумму повторно.
    """
    await assert_bank_funds(
        req.from_bank_account_id,
        req.client_id,
        req.initial_amount,
        "Пополнение брокерского счета возможно только со счета в валюте RUB",
    )
    idem_key = require_idempotency_key(request, req.idempotency_key)
    broker_id = None
    created_now = False
    try:
        async with invest_db() as idb:
            existing = await idb.scalar(select(BrokerAccount).where(BrokerAccount.idempotency_key == idem_key))
            stored = signed_response(existing, idem_key)
            if stored is not None:
                apply_receipt_headers(response, stored["signed_receipt"])
                return BrokerAccountResponse.model_validate(stored["body"])
            if getattr(existing, "idempotency_key", None) == idem_key:
                broker = existing
            else:
                next_comm_date = datetime.now() + relativedelta(months=1)
                account_number = await generate_unique_invest_account_number(idb, BrokerAccount, "3060")
                broker = BrokerAccount(
                    client_id=req.client_id,
                    account_number=account_number,
                    balance=req.initial_amount,
                    next_commission_date=next_comm_date,
                    idempotency_key=idem_key,
                )
                if broker.id is None:
                    broker.id = str(uuid.uuid4())
                idb.add(broker)
                await idb.commit()
                await idb.refresh(broker)
                created_now = True
            broker_id = str(broker.id)
            result = BrokerAccountResponse.model_validate(broker)
    except Exception as exc:
        log_error("InvestAPI", f"Ошибка создания брокерского счёта до списания: {exc}")
        if isinstance(exc, HTTPException):
            raise exc
        raise HTTPException(500, f"Ошибка создания брокерского счета: {exc}") from exc

    try:
        await debit_bank_once(
            req.from_bank_account_id,
            req.initial_amount,
            idem_key,
            "transfer_out_broker",
            "Пополнение брокерского счета",
        )
    except Exception as exc:
        if created_now:
            await _remove_invest_row(BrokerAccount, broker_id)
        if isinstance(exc, HTTPException):
            raise exc
        raise HTTPException(500, f"Списание за брокерский счёт не выполнено, актив отменён: {exc}") from exc

    request_nonce = None
    if request is not None:
        request_nonce = getattr(request.state, "enclave_nonce", None) or request.headers.get("X-Nonce")
    receipt = get_receipt_signer().create_and_sign_receipt(
        action="invest_broker_open",
        data={
            "client_id": req.client_id,
            "amount": str(req.initial_amount),
            "broker_id": broker_id,
            "account_number": result.account_number,
        },
        request_nonce=request_nonce,
    )
    payload = {"body": result.model_dump(mode="json"), "signed_receipt": receipt}
    await store_product_response(BrokerAccount, broker_id, idem_key, payload)
    apply_receipt_headers(response, receipt)
    return result


async def _eod_deposit_already_paid(deposit_id: str) -> bool:
    """Сообщает, что EOD уже записал выплату этого вклада.

    Аргументы:
        deposit_id: Идентификатор вклада.

    Возвращает:
        True, если в банке есть заявка ``eod-mature`` на этот вклад.
    """
    from src.simulations.db.bank.db.models import BankIdempotencyClaim

    eod_key = f"eod-mature:{deposit_id}"
    async with bank_db() as bdb:
        existing = await bdb.scalar(select(BankIdempotencyClaim).where(BankIdempotencyClaim.key == eod_key))
    return getattr(existing, "key", None) == eod_key


async def _clear_payout_intent(model_cls: type, row_id: str, key: str) -> None:
    """Снимает намерение выплаты, если деньги ещё не ушли.

    Аргументы:
        model_cls: Модель вклада или копилки.
        row_id: Идентификатор строки.
        key: Ключ, которым отмечено намерение.

    Побочные эффекты:
        Очищает payout_key, если продукт ещё не закрыт.
    """
    async with invest_db() as idb:
        row = await idb.scalar(select(model_cls).where(model_cls.id == row_id))
        if getattr(row, "payout_key", None) == key and getattr(row, "status", None) != "closed":
            row.payout_key = None
            row.payout_amount = None
            await idb.commit()


def emit_close_response(payload: dict, response: Response | None) -> dict:
    """Возвращает сохранённый ответ закрытия вклада с суммой в Decimal.

    Аргументы:
        payload: JSON первого выполнения. Сумма в нём хранится строкой.
        response: HTTP-ответ для подписи чека.

    Возвращает:
        Статус, сумма возврата и подписанный чек.
    """
    apply_receipt_headers(response, payload["signed_receipt"])
    return {
        "status": "success",
        "returned_amount": Decimal(str(payload["returned_amount"])),
        "signed_receipt": payload["signed_receipt"],
    }


async def credit_deposit_return(
    account_id: str,
    amount: Decimal,
    key: str,
    owner_client_id: str,
    forbidden_detail: str = "Банковский счет зачисления не принадлежит владельцу вклада",
    currency_error: str = "Возврат средств по вкладу возможен только на счет в валюте RUB",
    description: str = "Закрытие вклада",
    operation_type: str = "transfer_in_invest",
) -> bool:
    """Зачисляет возврат продукта на банковский счёт один раз.

    Аргументы:
        account_id: Счёт зачисления.
        amount: Сумма, снятая с продукта до закрытия сессии.
        key: Ключ идемпотентности закрытия.
        owner_client_id: Владелец продукта строкой.
        forbidden_detail: Текст отказа, если счёт чужой.
        currency_error: Текст отказа, если счёт не в рублях.
        description: Назначение проводки.
        operation_type: Тип операции, который входит в отпечаток ключа.

    Возвращает:
        True, если проводка создана этим вызовом. False, если это точный повтор.

    Вызывает:
        HTTPException: Счёт не найден, чужой, неактивен или не в рублях.

    Побочные эффекты:
        Увеличивает баланс и пишет доходную проводку.
    """
    async with bank_db() as bdb:
        should_post = await begin_bank_movement(
            bdb,
            key=key,
            operation_type=operation_type,
            account_id=account_id,
            amount=amount,
            direction="income",
        )
        if not should_post:
            return False
        acc = await bdb.scalar(select(Account).where(Account.id == account_id).with_for_update())
        if not acc:
            raise HTTPException(404, "Банковский счет не найден")
        if acc.client_id and owner_client_id and str(acc.client_id) != owner_client_id:
            raise HTTPException(403, forbidden_detail)
        if acc.status and acc.status != "active":
            raise HTTPException(400, f"Банковский счет зачисления недоступен (статус: {acc.status})")
        if acc.currency and acc.currency != "RUB":
            raise HTTPException(400, currency_error)
        acc.balance = Decimal(str(acc.balance)) + amount
        bdb.add(
            Transaction(
                account_id=acc.id,
                category="income",
                operation_type=operation_type,
                amount=amount,
                description=description,
                idempotency_key=key,
            )
        )
        remember_bank_movement(
            bdb,
            key=key,
            operation_type=operation_type,
            account_id=str(acc.id),
            amount=amount,
            direction="income",
        )
        await bdb.commit()
        return True


async def reverse_deposit_credit(
    account_id: str,
    amount: Decimal,
    key: str,
    operation_type: str = "compensation_deposit_close",
    description: str = "Откат зачисления: закрытие вклада не зафиксировано",
) -> None:
    """Снимает зачисление, если закрытие продукта после него не зафиксировано.

    Аргументы:
        account_id: Счёт, на который только что зачислили возврат.
        amount: Сумма отката.
        key: Ключ закрытия. Откат получает отдельный ключ проводки.
        operation_type: Тип проводки отката.
        description: Назначение отката.

    Вызывает:
        RuntimeError: Счёт не найден или баланса не хватает на откат.

    Побочные эффекты:
        Уменьшает баланс банка и не создаёт второе зачисление.
    """
    async with bank_db() as bdb:
        acc = await bdb.scalar(select(Account).where(Account.id == account_id).with_for_update())
        if acc is None:
            raise RuntimeError("Откат зачисления не выполнен: счёт не найден")
        balance = Decimal(str(acc.balance))
        if balance < amount:
            raise RuntimeError("Откат зачисления не выполнен: на счёте меньше суммы возврата")
        acc.balance = balance - amount
        await release_bank_claim(bdb, key)
        bdb.add(
            Transaction(
                account_id=acc.id,
                category="expense",
                operation_type=operation_type,
                amount=amount,
                description=description,
            )
        )
        await bdb.commit()


@router.post("/deposits/{deposit_id}/close")
async def close_deposit(
    deposit_id: str,
    req: CloseProductRequest,
    request: Request = None,
    response: Response = None,
):
    """Зачисляет остаток вклада на банковский счёт и только затем фиксирует закрытие.

    Повтор с тем же ключом возвращает сохранённый чек и не зачисляет сумму второй раз.

    Аргументы:
        deposit_id: Идентификатор вклада.
        req: Счёт зачисления и ключ идемпотентности.
        request: HTTP-запрос. В модульном тесте может отсутствовать.
        response: Ответ, в заголовки которого записывается подпись чека.

    Возвращает:
        Статус, сумма возврата и подписанный чек первого выполнения.

    Вызывает:
        HTTPException: Нет ключа, вклад не найден, уже закрыт или банк отклонил зачисление.

    Побочные эффекты:
        Создаёт доходную проводку банка и переводит вклад в статус closed.
    """
    idem_key = require_idempotency_key(request, req.idempotency_key)
    async with invest_db() as idb:
        dep = await idb.scalar(select(Deposit).where(Deposit.id == deposit_id).with_for_update())
        if not dep:
            raise HTTPException(404, "Вклад не найден")
        stored = signed_response(dep, idem_key, "close_idempotency_key", "close_response_payload")
        if stored is not None:
            return emit_close_response(stored, response)
        if dep.status == "closed":
            raise HTTPException(400, "Вклад уже закрыт")
        amount_to_return = Decimal(str(dep.balance))
        owner_client_id = str(dep.client_id) if dep.client_id else ""
        if dep.payout_key and dep.payout_key != idem_key:
            raise HTTPException(409, "Выплата по вкладу уже начата другим ключом")
        if not dep.payout_key:
            dep.payout_key = idem_key
            dep.payout_amount = canonical_amount(amount_to_return)
        else:
            amount_to_return = Decimal(str(dep.payout_amount))
        await idb.commit()

    if await _eod_deposit_already_paid(deposit_id):
        inserted = False
    else:
        inserted = False
        try:
            inserted = await credit_deposit_return(
                req.to_bank_account_id,
                amount_to_return,
                idem_key,
                owner_client_id,
            )
        except HTTPException:
            await _clear_payout_intent(Deposit, deposit_id, idem_key)
            raise
        except Exception as exc:
            await _clear_payout_intent(Deposit, deposit_id, idem_key)
            raise HTTPException(500, f"Зачисление закрытия вклада не выполнено: {exc}") from exc

    try:
        async with invest_db() as idb:
            dep = await idb.scalar(select(Deposit).where(Deposit.id == deposit_id).with_for_update())
            if not dep:
                raise HTTPException(404, "Вклад не найден")
            stored = signed_response(dep, idem_key, "close_idempotency_key", "close_response_payload")
            if stored is not None:
                return emit_close_response(stored, response)
            if dep.status == "closed":
                raise HTTPException(400, "Вклад уже закрыт")
            if dep.payout_key != idem_key and not await _eod_deposit_already_paid(deposit_id):
                raise HTTPException(409, "Выплата по вкладу уже начата другим ключом")
            amount_now = Decimal(str(dep.payout_amount or dep.balance))
            dep.status = "closed"
            dep.balance = Decimal("0.00")
            dep.close_idempotency_key = idem_key
            request_nonce = None
            if request is not None:
                request_nonce = getattr(request.state, "enclave_nonce", None) or request.headers.get("X-Nonce")
            receipt = get_receipt_signer().create_and_sign_receipt(
                action="invest_deposit_close",
                data={
                    "deposit_id": deposit_id,
                    "returned_amount": str(amount_now),
                    "to_bank_account_id": req.to_bank_account_id,
                },
                request_nonce=request_nonce,
            )
            payload = {
                "status": "success",
                "returned_amount": str(amount_now),
                "signed_receipt": receipt,
            }
            dep.close_response_payload = json.dumps(payload, ensure_ascii=False)
            await idb.commit()
    except Exception as exc:
        if inserted:
            await reverse_deposit_credit(req.to_bank_account_id, amount_to_return, idem_key)
        if isinstance(exc, HTTPException):
            raise exc
        raise HTTPException(500, f"Закрытие вклада не зафиксировано, зачисление отменено: {exc}") from exc

    log_info("InvestAPI", f"Вклад {deposit_id} закрыт. На счет возвращено: {amount_to_return}")
    return emit_close_response(payload, response)


@router.post("/savings/{savings_id}/close")
async def close_savings(
    savings_id: str,
    req: CloseProductRequest,
    request: Request = None,
    response: Response = None,
):
    """Зачисляет остаток копилки на банковский счёт и только затем фиксирует закрытие.

    Повтор с тем же ключом возвращает сохранённый чек и не зачисляет сумму второй раз.
    Если фиксация закрытия падает после зачисления, зачисление откатывается.

    Аргументы:
        savings_id: Идентификатор накопительного счёта.
        req: Счёт зачисления и ключ идемпотентности.
        request: HTTP-запрос. В модульном тесте может отсутствовать.
        response: Ответ, в заголовки которого записывается подпись чека.

    Возвращает:
        Статус, сумма возврата и подписанный чек первого выполнения.

    Вызывает:
        HTTPException: Нет ключа, копилка не найдена, уже закрыта или банк отклонил зачисление.

    Побочные эффекты:
        Создаёт доходную проводку банка и переводит копилку в статус closed.
    """
    idem_key = require_idempotency_key(request, req.idempotency_key)
    async with invest_db() as idb:
        sav = await idb.scalar(select(SavingsAccount).where(SavingsAccount.id == savings_id).with_for_update())
        if not sav:
            raise HTTPException(404, "Накопительный счет не найден")
        stored = signed_response(sav, idem_key, "close_idempotency_key", "close_response_payload")
        if stored is not None:
            return emit_close_response(stored, response)
        if sav.status == "closed":
            raise HTTPException(400, "Счет уже закрыт")
        amount_to_return = Decimal(str(sav.balance))
        owner_client_id = str(sav.client_id) if sav.client_id else ""
        if sav.payout_key and sav.payout_key != idem_key:
            raise HTTPException(409, "Выплата по копилке уже начата другим ключом")
        if not sav.payout_key:
            sav.payout_key = idem_key
            sav.payout_amount = canonical_amount(amount_to_return)
        else:
            amount_to_return = Decimal(str(sav.payout_amount))
        await idb.commit()

    inserted = False
    try:
        inserted = await credit_deposit_return(
            req.to_bank_account_id,
            amount_to_return,
            idem_key,
            owner_client_id,
            forbidden_detail="Банковский счет зачисления не принадлежит владельцу накопительного счета",
            currency_error="Возврат средств по накопительному счету возможен только на счет в валюте RUB",
            description="Закрытие накопительного счета",
            operation_type="transfer_in_savings",
        )
    except HTTPException:
        await _clear_payout_intent(SavingsAccount, savings_id, idem_key)
        raise
    except Exception as exc:
        await _clear_payout_intent(SavingsAccount, savings_id, idem_key)
        raise HTTPException(500, f"Зачисление закрытия копилки не выполнено: {exc}") from exc

    try:
        async with invest_db() as idb:
            sav = await idb.scalar(select(SavingsAccount).where(SavingsAccount.id == savings_id).with_for_update())
            if not sav:
                raise HTTPException(404, "Накопительный счет не найден")
            stored = signed_response(sav, idem_key, "close_idempotency_key", "close_response_payload")
            if stored is not None:
                return emit_close_response(stored, response)
            if sav.status == "closed":
                raise HTTPException(400, "Счет уже закрыт")
            if sav.payout_key != idem_key:
                raise HTTPException(409, "Выплата по копилке уже начата другим ключом")
            amount_now = Decimal(str(sav.payout_amount or sav.balance))
            sav.status = "closed"
            sav.balance = Decimal("0.00")
            sav.close_idempotency_key = idem_key
            request_nonce = None
            if request is not None:
                request_nonce = getattr(request.state, "enclave_nonce", None) or request.headers.get("X-Nonce")
            receipt = get_receipt_signer().create_and_sign_receipt(
                action="invest_savings_close",
                data={
                    "savings_id": savings_id,
                    "returned_amount": str(amount_now),
                    "to_bank_account_id": req.to_bank_account_id,
                },
                request_nonce=request_nonce,
            )
            payload = {
                "status": "success",
                "returned_amount": str(amount_now),
                "signed_receipt": receipt,
            }
            sav.close_response_payload = json.dumps(payload, ensure_ascii=False)
            await idb.commit()
    except Exception as exc:
        if inserted:
            await reverse_deposit_credit(
                req.to_bank_account_id,
                amount_to_return,
                idem_key,
                operation_type="compensation_savings_close",
                description="Откат зачисления: закрытие копилки не зафиксировано",
            )
        if isinstance(exc, HTTPException):
            raise exc
        raise HTTPException(500, f"Закрытие копилки не зафиксировано, зачисление отменено: {exc}") from exc

    log_info("InvestAPI", f"Копилка {savings_id} закрыта. На счет возвращено: {amount_to_return}")
    return emit_close_response(payload, response)


@router.post("/broker-accounts/{account_id}/strategy/subscribe")
async def subscribe_strategy(account_id: str, req: StrategySubscription):
    """
    Подключить брокерский счет к инвестиционной стратегии.
    Воркеры будут использовать эту стратегию для начисления доходности.
    """
    async with invest_db() as idb:
        acc = await idb.scalar(select(BrokerAccount).where(BrokerAccount.id == account_id))
        if not acc:
            raise HTTPException(404, "Брокерский счет не найден")

        acc.strategy_id = req.strategy_id
        if not acc.next_commission_date:
            acc.next_commission_date = datetime.now() + relativedelta(months=1)
        await idb.commit()
        log_info(
            "InvestAPI",
            f"Брокерский счет {account_id} подписан на стратегию {req.strategy_id}",
        )
        return {"status": "success"}


@router.post("/broker-accounts/{account_id}/strategy/unsubscribe")
async def unsubscribe_strategy(account_id: str):
    """
    Отключить брокерский счет от текущей инвестиционной стратегии.
    """
    async with invest_db() as idb:
        acc = await idb.scalar(select(BrokerAccount).where(BrokerAccount.id == account_id))
        if not acc:
            raise HTTPException(404, "Брокерский счет не найден")

        acc.strategy_id = None
        await idb.commit()
        log_info("InvestAPI", f"Брокерский счет {account_id} отписан от стратегии")
        return {"status": "success"}
