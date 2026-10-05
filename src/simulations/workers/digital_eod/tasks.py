from datetime import datetime, timezone, timedelta
from decimal import Decimal
from sqlalchemy.future import select

from src.simulations.db.digital_ruble.db.client import get_async_session_maker
AsyncSessionLocal = get_async_session_maker()

from src.simulations.db.digital_ruble.db.models import SmartContract, Wallet, RubleTransaction
from src.simulations.smart_contracts.dsl.context import ContractContext
from src.simulations.smart_contracts.sandbox.executor import SmartContractSandbox
from src.simulations.core.utils.console_logger import log_info, log_error, log_success
from src.simulations.core.crypto import get_digital_platform_signer



def get_moscow_now() -> datetime:
    """Возвращает текущее время по московскому поясу (UTC+3)."""
    return datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)


async def process_single_contract(session, contract: SmartContract) -> str:
    """
    Обрабатывает один смарт-контракт в изолированном savepoint.
    Возвращает статус: 'executed', 'failed', 'pending', 'skipped'.
    """
    contract_amount = Decimal(str(contract.amount))

    # Блокируем строки кошельков участников от параллельных изменений
    creator_res = await session.execute(
        select(Wallet).where(Wallet.id == contract.creator_wallet_id).with_for_update()
    )
    creator = creator_res.scalars().first()

    receiver_res = await session.execute(
        select(Wallet).where(Wallet.id == contract.receiver_wallet_id).with_for_update()
    )
    receiver = receiver_res.scalars().first()

    if not creator or not receiver:
        contract.status = "failed"
        contract.error_message = "Кошелек создателя или получателя не найден в базе данных"
        return "failed"

    # Подготавливаем контекст песочницы
    ctx = ContractContext(
        contract_id=contract.id,
        creator_id=contract.creator_wallet_id,
        receiver_id=contract.receiver_wallet_id,
        amount=contract_amount,
        condition_status=contract.condition_status
    )

    # Исполняем код контракта в защищенной песочнице
    success, is_completed, transfers, error_msg = SmartContractSandbox.execute_contract(
        code=contract.contract_code or "",
        ctx=ctx
    )

    creator_frozen = Decimal(str(creator.frozen_balance))
    creator_bal = Decimal(str(creator.balance))
    receiver_bal = Decimal(str(receiver.balance))

    if not success:
        # Контракт завершился ошибкой (синтаксис, запрещенный вызов, таймаут)
        contract.status = "failed"
        contract.error_message = error_msg

        # Возвращаем заблокированные средства создателю на его основной баланс
        refund_amount = min(creator_frozen, contract_amount)
        creator.frozen_balance = creator_frozen - refund_amount
        creator.balance = creator_bal + refund_amount
        return "failed"

    elif is_completed:
        # Контракт успешно выполнен и запросил расчет
        transferred_total = Decimal("0.00")

        # Применяем переводы, списав средства из замороженного залога создателя
        for tx in transfers:
            amount_to_send = Decimal(str(tx["amount"]))
            to_wallet_id = tx["to"]

            if str(to_wallet_id) == str(receiver.id):
                # Перевод получателю
                receiver_bal += amount_to_send
                to_wallet = receiver
            elif str(to_wallet_id) == str(creator.id):
                # Возврат создателю
                creator_bal += amount_to_send
                to_wallet = creator
            else:
                continue

            transferred_total += amount_to_send

            # Записываем операцию в историю транзакций с реальной криптографической подписью платформы
            sig_ts = int(datetime.now(timezone.utc).timestamp())
            signature = get_digital_platform_signer().sign_transaction(
                sender_id=creator.id,
                receiver_id=to_wallet.id,
                amount=amount_to_send,
                contract_id=contract.id,
                timestamp=sig_ts,
            )
            new_tx = RubleTransaction(
                sender_wallet_id=creator.id,
                receiver_wallet_id=to_wallet.id,
                amount=amount_to_send,
                status="completed",
                smart_contract_id=contract.id,
                signature=signature,
                timestamp=get_moscow_now()
            )
            session.add(new_tx)


        # Снимаем из frozen_balance фактический объем переводов
        escrow_release = min(creator_frozen, contract_amount)
        creator_frozen -= escrow_release

        # Если контракт израсходовал не всю сумму залога, возвращаем остаток на баланс создателя
        unspent_amount = contract_amount - transferred_total
        if unspent_amount > Decimal("0.00"):
            creator_bal += unspent_amount

        # Сохраняем итоговые балансы
        creator.balance = creator_bal
        creator.frozen_balance = creator_frozen
        receiver.balance = receiver_bal

        contract.status = "executed"
        contract.executed_at = get_moscow_now()
        contract.error_message = None
        return "executed"

    else:
        # Условие еще не выполнено: контракт остается активным до следующего прогона
        return "pending"


async def process_smart_contracts(session):
    """
    Извлекает активные смарт-контракты и исполняет каждый в отдельном savepoint.
    """
    log_info("Ruble EOD", "Поиск активных смарт-контрактов для исполнения...")

    result = await session.execute(
        select(SmartContract).where(SmartContract.status == "active")
    )
    contracts = result.scalars().all()

    processed = 0
    executed = 0
    failed = 0
    pending = 0

    for contract in contracts:
        processed += 1
        try:
            async with session.begin_nested():
                status = await process_single_contract(session, contract)
                if status == "executed":
                    executed += 1
                elif status == "failed":
                    failed += 1
                elif status == "pending":
                    pending += 1
        except Exception as e:
            failed += 1
            log_error("Ruble EOD", f"Критический сбой при обработке контракта {contract.id}: {e}")
            contract.status = "failed"
            contract.error_message = f"Внутренний сбой обработчика: {e}"

    await session.commit()
    log_info(
        "Ruble EOD",
        f"Обработано контрактов: {processed}. Исполнено: {executed}. В ожидании: {pending}. Ошибок: {failed}."
    )


async def run_ruble_eod():
    """
    Точка входа запуска ночного цикла расчетов цифрового рубля.
    """
    async with AsyncSessionLocal() as session:
        log_info("Ruble EOD", "=== НАЧАЛО EOD ЦИФРОВОГО РУБЛЯ ===")
        await process_smart_contracts(session)
        log_info("Ruble EOD", "=== EOD ЦИФРОВОГО РУБЛЯ ЗАВЕРШЕН ===")
