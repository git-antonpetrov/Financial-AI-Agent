import json
import os
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.future import select

from src.simulations.api.bank_claims import begin_bank_movement, release_bank_claim, remember_bank_movement
from src.simulations.api.core.auth import verify_digital_token, verify_oracle_token
from src.simulations.api.schemas.responses import (
    RubleTransactionResponse,
    SmartContractResponse,
    WalletResponse,
)
from src.simulations.core.crypto import (
    get_digital_platform_signer,
    get_oracle_verifier,
    get_receipt_signer,
)
from src.simulations.core.utils.console_logger import log_error, log_info, log_success
from src.simulations.db.bank.db.client import (
    get_async_session_maker as get_bank_session,
)
from src.simulations.db.bank.db.models import Account, Transaction
from src.simulations.db.digital_ruble.db.client import (
    get_async_session_maker as get_ruble_session,
)
from src.simulations.db.digital_ruble.db.models import (
    RubleTransaction,
    SmartContract,
    Wallet,
)

router = APIRouter(dependencies=[Depends(verify_digital_token)])


def ruble_db():
    """Фабрика для получения сессии БД цифрового рубля."""
    return get_ruble_session()()


def bank_db():
    """Фабрика для получения сессии банковской БД."""
    return get_bank_session()()


def _require_ruble_key(request: Request | None, explicit: str | None) -> str:
    """Возвращает ключ идемпотентности рублёвой межкнижной проводки.

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


def _signed_ruble_response(row: object, key: str) -> dict | None:
    """Возвращает сохранённый подписанный ответ рублёвой проводки.

    Аргументы:
        row: Транзакция с ключом идемпотентности. Пустая строка означает, что повтора нет.
        key: Ключ текущего запроса.

    Возвращает:
        Словарь ответа с полем signed_receipt либо None.

    Вызывает:
        ValueError: Сохранённый JSON не содержит чек.
    """
    if getattr(row, "idempotency_key", None) != key:
        return None
    raw = getattr(row, "response_payload", None)
    if not isinstance(raw, str) or not raw.strip():
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("signed_receipt"), dict):
        raise ValueError("Сохранённый ответ не содержит подписанный чек")
    return parsed


def _apply_ruble_receipt(response: Response | None, receipt: dict) -> None:
    """Записывает подпись сохранённого чека в заголовки ответа.

    Аргументы:
        response: HTTP-ответ. В модульном тесте может отсутствовать.
        receipt: Чек первого выполнения.
    """
    if response is None:
        return
    signature = receipt.get("signature")
    if isinstance(signature, str) and signature:
        response.headers["X-Bank-Signature"] = signature


async def _store_ruble_response(key: str, payload: dict) -> None:
    """Сохраняет подписанный ответ на рублёвой транзакции с этим ключом.

    Аргументы:
        key: Ключ идемпотентности.
        payload: Ответ, который получит клиент.

    Побочные эффекты:
        Обновляет response_payload. Не записывает чужую строку, если повторная загрузка её не находит.
    """
    raw = json.dumps(payload, ensure_ascii=False)
    async with ruble_db() as rdb:
        row = await rdb.scalar(select(RubleTransaction).where(RubleTransaction.idempotency_key == key))
        if getattr(row, "idempotency_key", None) != key:
            return
        row.response_payload = raw
        await rdb.commit()


async def _debit_bank_for_ruble(
    account_id: str,
    amount: Decimal,
    key: str,
    owner_client_id: str | None,
) -> None:
    """Списывает банк один раз после зачисления на кошелёк.

    Аргументы:
        account_id: Счёт списания.
        amount: Сумма.
        key: Ключ идемпотентности.
        owner_client_id: Клиент счёта, сверенный до закрытия сессии проверки.
    """
    async with bank_db() as bdb:
        should_post = await begin_bank_movement(
            bdb,
            key=key,
            operation_type="transfer_out_ruble",
            account_id=account_id,
            amount=amount,
            direction="expense",
        )
        if not should_post:
            return
        acc = await bdb.scalar(select(Account).where(Account.id == account_id).with_for_update())
        if not acc:
            raise HTTPException(404, "Банковский счет не найден")
        if owner_client_id and acc.client_id and str(acc.client_id) != owner_client_id:
            raise HTTPException(403, "Банковский счет и кошелек принадлежат разным клиентам")
        balance = Decimal(str(acc.balance))
        if balance < amount:
            raise HTTPException(400, "Недостаточно средств на банковском счете")
        acc.balance = balance - amount
        bdb.add(
            Transaction(
                account_id=acc.id,
                category="expense",
                operation_type="transfer_out_ruble",
                amount=amount,
                description="Пополнение кошелька цифрового рубля",
                idempotency_key=key,
            )
        )
        remember_bank_movement(
            bdb,
            key=key,
            operation_type="transfer_out_ruble",
            account_id=str(acc.id),
            amount=amount,
            direction="expense",
        )
        await bdb.commit()


async def _credit_bank_for_ruble(
    account_id: str,
    amount: Decimal,
    key: str,
    wallet_client_id: str | None,
) -> None:
    """Зачисляет вывод на банковский счёт один раз.

    Аргументы:
        account_id: Счёт зачисления.
        amount: Сумма.
        key: Ключ идемпотентности.
        wallet_client_id: Владелец кошелька, снятый до закрытия сессии кошелька.

    Вызывает:
        HTTPException: Счёт не найден, чужой или не в рублях.
    """
    async with bank_db() as bdb:
        should_post = await begin_bank_movement(
            bdb,
            key=key,
            operation_type="transfer_in_ruble",
            account_id=account_id,
            amount=amount,
            direction="income",
        )
        if not should_post:
            return
        acc = await bdb.scalar(select(Account).where(Account.id == account_id).with_for_update())
        if not acc:
            raise HTTPException(404, "Банковский счет не найден")
        if acc.status and acc.status != "active":
            raise HTTPException(400, f"Банковский счет недоступен (статус: {acc.status})")
        if acc.currency and acc.currency != "RUB":
            raise HTTPException(400, "Вывод цифрового рубля возможен только на рублевый банковский счет")
        if acc.client_id and wallet_client_id and str(acc.client_id) != wallet_client_id:
            raise HTTPException(403, "Банковский счет и кошелек принадлежат разным клиентам")
        acc.balance = Decimal(str(acc.balance)) + amount
        bdb.add(
            Transaction(
                account_id=acc.id,
                category="income",
                operation_type="transfer_in_ruble",
                amount=amount,
                description="Вывод из кошелька цифрового рубля",
                idempotency_key=key,
            )
        )
        remember_bank_movement(
            bdb,
            key=key,
            operation_type="transfer_in_ruble",
            account_id=str(acc.id),
            amount=amount,
            direction="income",
        )
        await bdb.commit()


# --- Схемы данных (Pydantic) ---


class FundWithdrawRequest(BaseModel):
    """Схема запроса для пополнения и вывода средств из кошелька цифрового рубля."""

    bank_account_id: str
    amount: Decimal = Field(..., gt=Decimal("0.00"), description="Сумма операции (строго больше нуля)")
    idempotency_key: str | None = None


class RubleTransferRequest(BaseModel):
    """Схема запроса для P2P перевода цифровых рублей."""

    to_wallet_id: str
    amount: Decimal = Field(..., gt=Decimal("0.00"), description="Сумма P2P перевода (строго больше нуля)")
    idempotency_key: str | None = None


class CreateContractRequest(BaseModel):
    """Схема запроса для создания смарт-контракта."""

    receiver_wallet_id: str
    amount: Decimal = Field(
        ...,
        gt=Decimal("0.00"),
        description="Сумма смарт-контракта (строго больше нуля)",
    )
    condition_type: str
    contract_code: str
    idempotency_key: str | None = None


class ConditionUpdateRequest(BaseModel):
    """Схема запроса для обновления внешнего условия смарт-контракта оракулом."""

    status: str
    oracle_name: str | None = "default_oracle"
    reason: str | None = None
    signature: str | None = None
    nonce: str | None = None
    timestamp: float | int | str | None = None


# --- Вспомогательные функции компенсации (Saga pattern) ---


async def _reverse_bank_credit(account_id: str, amount: Decimal, key: str) -> None:
    """Снимает зачисление банка, если списание кошелька после него не прошло.

    Кошелёк не меняет.

    Аргументы:
        account_id: Счёт, на который только что зачислили вывод.
        amount: Сумма этого зачисления.
        key: Ключ идемпотентности вывода.

    Вызывает:
        RuntimeError: Счёт не найден или баланса не хватает на откат.

    Побочные эффекты:
        Уменьшает баланс на сумму зачисления и удаляет проводку с этим ключом.
    """
    async with bank_db() as bdb:
        acc = await bdb.scalar(select(Account).where(Account.id == account_id).with_for_update())
        if acc is None:
            raise RuntimeError("Откат зачисления вывода не выполнен: счёт не найден")
        balance = Decimal(str(acc.balance))
        if balance < amount:
            raise RuntimeError("Откат зачисления вывода не выполнен: на счёте меньше суммы зачисления")
        acc.balance = balance - amount
        await release_bank_claim(bdb, key)
        await bdb.commit()


async def _reverse_wallet_funding(wallet_id: str, amount: Decimal, key: str) -> None:
    """Возвращает кошелёк к состоянию до этого пополнения.

    Банк не меняет: списание с банковского счёта не прошло.

    Аргументы:
        wallet_id: Кошелёк, на который только что зачислили сумму.
        amount: Сумма этого пополнения.
        key: Ключ идемпотентности пополнения.

    Вызывает:
        RuntimeError: Кошелёк не найден или баланса не хватает, чтобы снять пополнение.

    Побочные эффекты:
        Уменьшает баланс кошелька на сумму пополнения и удаляет рублёвую проводку с этим ключом.
    """
    async with ruble_db() as rdb:
        wallet = await rdb.scalar(select(Wallet).where(Wallet.id == wallet_id).with_for_update())
        if wallet is None:
            raise RuntimeError("Откат пополнения кошелька не выполнен: кошелёк не найден")
        balance = Decimal(str(wallet.balance))
        if balance < amount:
            raise RuntimeError("Откат пополнения кошелька не выполнен: на кошельке меньше суммы пополнения")
        wallet.balance = balance - amount
        existing = await rdb.scalar(select(RubleTransaction).where(RubleTransaction.idempotency_key == key))
        if getattr(existing, "idempotency_key", None) == key:
            await rdb.delete(existing)
        await rdb.commit()


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


@router.get("/wallets/{wallet_id}/transactions", response_model=list[RubleTransactionResponse])
async def get_transactions(wallet_id: str) -> list[RubleTransactionResponse]:
    """
    Получить историю транзакций для кошелька цифрового рубля.
    """
    async with ruble_db() as db:
        result = await db.execute(
            select(RubleTransaction)
            .where(
                (RubleTransaction.sender_wallet_id == wallet_id) | (RubleTransaction.receiver_wallet_id == wallet_id)
            )
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
    """Зачисляет сумму на кошелёк до списания с банковского счёта.

    Повтор с тем же ключом возвращает подписанный ответ первого выполнения и не списывает сумму второй раз.
    """
    idem_key = _require_ruble_key(request, req.idempotency_key)
    async with ruble_db() as rdb:
        existing = await rdb.scalar(select(RubleTransaction).where(RubleTransaction.idempotency_key == idem_key))
        stored = _signed_ruble_response(existing, idem_key)
    if stored is not None:
        _apply_ruble_receipt(response, stored["signed_receipt"])
        return stored
    async with bank_db() as bdb:
        acc = await bdb.scalar(select(Account).where(Account.id == req.bank_account_id).with_for_update())
        if not acc:
            raise HTTPException(404, "Банковский счет не найден")
        if acc.status and acc.status != "active":
            raise HTTPException(400, f"Банковский счет недоступен (статус: {acc.status})")
        if acc.currency and acc.currency != "RUB":
            raise HTTPException(400, "Пополнение кошелька цифрового рубля возможно только с рублевого счета")
        if Decimal(str(acc.balance)) < req.amount:
            raise HTTPException(400, "Недостаточно средств на банковском счете")
        bank_client_id = str(acc.client_id) if acc.client_id else None
    credited = False
    try:
        async with ruble_db() as rdb:
            existing = await rdb.scalar(
                select(RubleTransaction).where(RubleTransaction.idempotency_key == idem_key)
            )
            wallet = await rdb.scalar(select(Wallet).where(Wallet.id == wallet_id).with_for_update())
            if not wallet:
                raise HTTPException(404, "Кошелек не найден")
            if wallet.status and wallet.status != "active":
                raise HTTPException(400, f"Кошелек недоступен (статус: {wallet.status})")
            if bank_client_id and wallet.client_id and str(bank_client_id) != str(wallet.client_id):
                raise HTTPException(403, "Банковский счет и кошелек принадлежат разным клиентам")
            if getattr(existing, "idempotency_key", None) != idem_key:
                wallet.balance = Decimal(str(wallet.balance)) + req.amount
                rdb.add(
                    RubleTransaction(
                        receiver_wallet_id=wallet.id,
                        amount=req.amount,
                        idempotency_key=idem_key,
                        status="completed",
                    )
                )
                await rdb.commit()
                credited = True
    except Exception as exc:
        log_error("RubleAPI", f"Ошибка зачисления на кошелёк {wallet_id} до списания: {exc}")
        if isinstance(exc, HTTPException):
            raise exc
        raise HTTPException(500, f"Ошибка пополнения цифрового кошелька: {exc}") from exc

    try:
        await _debit_bank_for_ruble(req.bank_account_id, req.amount, idem_key, bank_client_id)
    except Exception as exc:
        if credited:
            await _reverse_wallet_funding(wallet_id, req.amount, idem_key)
        if isinstance(exc, HTTPException):
            raise exc
        raise HTTPException(500, f"Списание за пополнение кошелька не выполнено: {exc}") from exc

    log_success(
        "RubleAPI",
        f"Кошелек {wallet_id} пополнен на {req.amount} со счета {req.bank_account_id}",
    )
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
    payload = {"status": "success", "signed_receipt": receipt}
    await _store_ruble_response(idem_key, payload)
    _apply_ruble_receipt(response, receipt)
    return payload


@router.post("/wallets/{wallet_id}/withdraw")
async def withdraw_wallet(
    wallet_id: str,
    req: FundWithdrawRequest,
    request: Request = None,
    response: Response = None,
):
    """Зачисляет вывод на банковский счёт до списания с кошелька.

    Повтор с тем же ключом возвращает подписанный ответ первого выполнения
    и не зачисляет сумму на банковский счёт второй раз.
    """
    idem_key = _require_ruble_key(request, req.idempotency_key)
    async with ruble_db() as rdb:
        existing = await rdb.scalar(select(RubleTransaction).where(RubleTransaction.idempotency_key == idem_key))
        stored = _signed_ruble_response(existing, idem_key)
        if stored is not None:
            wallet_client_id = None
        else:
            wallet = await rdb.scalar(select(Wallet).where(Wallet.id == wallet_id).with_for_update())
            if not wallet:
                raise HTTPException(404, "Кошелек не найден")
            if wallet.status and wallet.status != "active":
                raise HTTPException(400, f"Кошелек недоступен (статус: {wallet.status})")
            if Decimal(str(wallet.balance)) < req.amount:
                raise HTTPException(400, "Недостаточно цифровых рублей")
            wallet_client_id = str(wallet.client_id) if wallet.client_id else None
    if stored is not None:
        _apply_ruble_receipt(response, stored["signed_receipt"])
        return stored
    credited = False
    try:
        await _credit_bank_for_ruble(req.bank_account_id, req.amount, idem_key, wallet_client_id)
        credited = True
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise exc
        raise HTTPException(500, f"Зачисление вывода на банковский счёт не выполнено: {exc}") from exc
    try:
        async with ruble_db() as rdb:
            existing = await rdb.scalar(
                select(RubleTransaction).where(RubleTransaction.idempotency_key == idem_key)
            )
            if getattr(existing, "idempotency_key", None) != idem_key:
                wallet = await rdb.scalar(select(Wallet).where(Wallet.id == wallet_id).with_for_update())
                if not wallet:
                    raise HTTPException(404, "Кошелек не найден")
                balance = Decimal(str(wallet.balance))
                if balance < req.amount:
                    raise HTTPException(400, "Недостаточно цифровых рублей")
                wallet.balance = balance - req.amount
                rdb.add(
                    RubleTransaction(
                        sender_wallet_id=wallet.id,
                        receiver_wallet_id=wallet.id,
                        amount=req.amount,
                        idempotency_key=idem_key,
                        status="completed",
                    )
                )
                await rdb.commit()
    except Exception as exc:
        if credited:
            await _reverse_bank_credit(req.bank_account_id, req.amount, idem_key)
        if isinstance(exc, HTTPException):
            raise exc
        raise HTTPException(500, f"Списание с кошелька после зачисления не выполнено: {exc}") from exc

    log_success(
        "RubleAPI",
        f"С кошелька {wallet_id} выведено {req.amount} на счет {req.bank_account_id}",
    )
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
    payload = {"status": "success", "signed_receipt": receipt}
    await _store_ruble_response(idem_key, payload)
    _apply_ruble_receipt(response, receipt)
    return payload


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
    idem_key = _require_ruble_key(request, req.idempotency_key)

    async with ruble_db() as db:
        existing = await db.scalar(select(RubleTransaction).where(RubleTransaction.idempotency_key == idem_key))
        if getattr(existing, "idempotency_key", None) == idem_key:
            stored = _signed_ruble_response(existing, idem_key)
            if stored is None:
                raise HTTPException(409, "Ключ идемпотентности уже использован другой операцией")
            return RubleTransactionResponse.model_validate(stored["body"])
        lock_ids = sorted([str(wallet_id), str(req.to_wallet_id)])
        wallets_res = await db.execute(
            select(Wallet).where(Wallet.id.in_(lock_ids)).order_by(Wallet.id).with_for_update()
        )
        wallets_map = {str(w.id): w for w in wallets_res.scalars().all()}

        sender = wallets_map.get(str(wallet_id))
        if not sender:
            raise HTTPException(404, "Отправитель не найден")
        if sender.status and sender.status != "active":
            raise HTTPException(400, f"Кошелек отправителя недоступен (статус: {sender.status})")

        receiver = wallets_map.get(str(req.to_wallet_id))
        if not receiver:
            raise HTTPException(404, "Получатель не найден")
        if receiver.status and receiver.status != "active":
            raise HTTPException(400, f"Кошелек получателя недоступен (статус: {receiver.status})")

        sender_balance = Decimal(str(sender.balance))
        if sender_balance < req.amount:
            raise HTTPException(400, "Недостаточно цифровых рублей")

        sender.balance = sender_balance - req.amount
        receiver.balance = Decimal(str(receiver.balance)) + req.amount

        # Криптографическая подпись транзакции ключом платформы ЦР
        sig_ts = int(datetime.now(UTC).timestamp())
        sig = get_digital_platform_signer().sign_transaction(
            sender_id=sender.id,
            receiver_id=receiver.id,
            amount=req.amount,
            contract_id="direct_p2p",
            timestamp=sig_ts,
        )

        result = RubleTransactionResponse.model_validate(
            {
                "sender_wallet_id": str(sender.id),
                "receiver_wallet_id": str(receiver.id),
                "amount": req.amount,
                "status": "completed",
                "signature": sig,
            }
        )
        rtx = RubleTransaction(
            sender_wallet_id=sender.id,
            receiver_wallet_id=receiver.id,
            amount=req.amount,
            signature=sig,
            idempotency_key=idem_key,
            response_payload=json.dumps(
                {"body": result.model_dump(mode="json"), "signed_receipt": {"signature": sig}},
                ensure_ascii=False,
            ),
        )
        db.add(rtx)
        await db.commit()
        await db.refresh(rtx)
        result = RubleTransactionResponse.model_validate(rtx)

        if response is not None:
            response.headers["X-Bank-Signature"] = sig
        log_success(
            "RubleAPI",
            f"P2P Перевод ЦР: {req.amount} от {wallet_id} к {req.to_wallet_id} (sig={sig[:16]}...)",
        )
        return result


@router.post("/wallets/{wallet_id}/smart-contracts", response_model=SmartContractResponse)
async def create_smart_contract(wallet_id: str, req: CreateContractRequest) -> SmartContractResponse:
    """
    Создать смарт-контракт. Средства замораживаются на счету инициатора
    до момента исполнения контракта.
    Использует with_for_update() для защиты баланса создателя от гонки.
    """
    if str(wallet_id) == str(req.receiver_wallet_id):
        raise HTTPException(400, "Создатель и получатель смарт-контракта не могут совпадать")
    idem_key = _require_ruble_key(None, req.idempotency_key)

    async with ruble_db() as db:
        existing = await db.scalar(select(SmartContract).where(SmartContract.idempotency_key == idem_key))
        if getattr(existing, "idempotency_key", None) == idem_key:
            raw = getattr(existing, "response_payload", None)
            if not isinstance(raw, str) or not raw.strip():
                raise HTTPException(409, "Ключ идемпотентности уже использован другой операцией")
            stored = json.loads(raw)
            return SmartContractResponse.model_validate(stored["body"])
        creator = await db.scalar(select(Wallet).where(Wallet.id == wallet_id).with_for_update())
        if not creator:
            raise HTTPException(404, "Создатель контракта не найден")
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
            contract_code=req.contract_code,
            idempotency_key=idem_key,
        )
        db.add(contract)
        await db.flush()
        stored_contract = SmartContractResponse.model_validate(contract)
        contract.response_payload = json.dumps(
            {"body": stored_contract.model_dump(mode="json"), "signed_receipt": {"signature": idem_key}},
            ensure_ascii=False,
        )
        await db.commit()
        await db.refresh(contract)

        log_info(
            "RubleAPI",
            f"Создан смарт-контракт от {wallet_id} для {req.receiver_wallet_id} на {req.amount}",
        )
        return SmartContractResponse.model_validate(contract)


@router.get("/wallets/{wallet_id}/smart-contracts", response_model=list[SmartContractResponse])
async def get_smart_contracts(wallet_id: str) -> list[SmartContractResponse]:
    """
    Получить список смарт-контрактов, связанных с кошельком (как инициатор или получатель).
    """
    async with ruble_db() as db:
        result = await db.execute(
            select(SmartContract).where(
                (SmartContract.creator_wallet_id == wallet_id) | (SmartContract.receiver_wallet_id == wallet_id)
            )
        )
        return [SmartContractResponse.model_validate(c) for c in result.scalars().all()]


@router.post(
    "/smart-contracts/{contract_id}/condition",
    dependencies=[Depends(verify_oracle_token)],
)
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
    if req.status not in ["fulfilled", "failed"]:
        raise HTTPException(400, "Неверный статус условия. Разрешены только 'fulfilled' или 'failed'")

    # Криптографическая валидация подписи оракула (SEC-08, SEC-14)
    if req.signature:
        nonce = req.nonce or (request.headers.get("X-Nonce") if request else None)
        timestamp = req.timestamp or (request.headers.get("X-Timestamp") if request else None)
        strict_sec = os.getenv("STRICT_SECURITY", "true").lower() in ("true", "1")
        default_req = "true" if strict_sec else "false"
        require_nonce_ts = os.getenv("REQUIRE_ORACLE_NONCE_TIMESTAMP", default_req).lower() in ("true", "1")
        if require_nonce_ts and (not nonce or not timestamp):
            raise HTTPException(
                400,
                detail="Для проверки подписи оракула обязательны заголовок/поле X-Nonce и X-Timestamp",
            )
        is_valid, sig_err = get_oracle_verifier().verify_condition_signature(
            contract_id=contract_id,
            status=req.status,
            oracle_name=req.oracle_name,
            signature_b64=req.signature,
            nonce=nonce,
            timestamp=timestamp,
        )
        if not is_valid:
            log_error(
                "RubleAPI",
                f"Недействительная цифровая подпись оракула для контракта {contract_id}: {sig_err}",
            )
            if "Replay-атака" in sig_err or "уже был использован" in sig_err:
                raise HTTPException(409, detail=sig_err)
            raise HTTPException(403, detail=f"Недействительная цифровая подпись оракула: {sig_err}")
    elif os.getenv("REQUIRE_ORACLE_SIGNATURE", "true").lower() in ("true", "1", "yes"):
        raise HTTPException(401, detail="Требуется цифровая подпись оракула")

    async with ruble_db() as db:
        contract = await db.scalar(select(SmartContract).where(SmartContract.id == contract_id).with_for_update())
        if not contract:
            log_error("RubleAPI", f"Оракул запросил несуществующий контракт {contract_id}")
            raise HTTPException(404, "Смарт-контракт не найден")

        if contract.status != "active":
            raise HTTPException(400, f"Контракт не активен (текущий статус: {contract.status})")

        if contract.condition_status in ["fulfilled", "failed"]:
            raise HTTPException(
                409,
                f"Условие контракта уже зафиксировано со статусом '{contract.condition_status}'",
            )

        contract.condition_status = req.status
        await db.commit()
        await db.refresh(contract)

        log_info(
            "RubleAPI",
            f"Оракул '{req.oracle_name}' обновил статус условия контракта {contract_id} на '{req.status}' "
            f"(причина: {req.reason or 'не указана'})",
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
