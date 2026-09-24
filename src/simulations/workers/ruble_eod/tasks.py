from datetime import datetime, timezone, timedelta
from sqlalchemy.future import select
from src.simulations.db.digital_ruble.db.client import get_async_session_maker
AsyncSessionLocal = get_async_session_maker()

from src.simulations.db.digital_ruble.db.models import SmartContract, Wallet, RubleTransaction
from src.simulations.smart_contracts.dsl.context import ContractContext
from src.simulations.smart_contracts.sandbox.executor import SmartContractSandbox
from src.core.utils.console_logger import log_info, log_error

async def process_smart_contracts(session):
    """
    Берет активные смарт-контракты из базы, прогоняет их через песочницу и фиксирует результаты.
    """
    log_info("Ruble EOD", "Поиск активных смарт-контрактов для исполнения...")
    
    # Ищем все контракты со статусом active
    result = await session.execute(
        select(SmartContract).where(SmartContract.status == "active")
    )
    contracts = result.scalars().all()
    
    processed = 0
    executed = 0
    failed = 0
    
    for contract in contracts:
        processed += 1
        
        # Получаем кошельки участников (для проверки балансов и начислений)
        creator_result = await session.execute(select(Wallet).where(Wallet.id == contract.creator_wallet_id))
        creator = creator_result.scalars().first()
        
        receiver_result = await session.execute(select(Wallet).where(Wallet.id == contract.receiver_wallet_id))
        receiver = receiver_result.scalars().first()
        
        if not creator or not receiver:
            contract.status = "failed"
            contract.error_message = "Кошелек создателя или получателя не найден"
            failed += 1
            continue
            
        # Подготавливаем изолированный контекст для песочницы
        ctx = ContractContext(
            contract_id=contract.id,
            creator_id=contract.creator_wallet_id,
            receiver_id=contract.receiver_wallet_id,
            amount=float(contract.amount),
            condition_status=contract.condition_status
        )
        
        # Запускаем песочницу
        success, is_completed, transfers, error_msg = SmartContractSandbox.execute_contract(
            code=contract.contract_code or "", 
            ctx=ctx
        )
        
        if not success:
            # Ошибка в коде смарт-контракта (синтаксис или рантайм)
            contract.status = "failed"
            contract.error_message = error_msg
            
            # Размораживаем средства, так как контракт упал и не будет исполнен
            if creator.frozen_balance >= contract.amount:
                creator.frozen_balance -= contract.amount
            else:
                creator.frozen_balance = 0
            failed += 1
            
        elif is_completed:
            # Контракт отработал успешно и заявил о завершении своей логики
            # Размораживаем всю сумму зарезервированную контрактом
            if creator.frozen_balance >= contract.amount:
                creator.frozen_balance -= contract.amount
            else:
                creator.frozen_balance = 0
            
            # Применяем все запрошенные переводы
            for tx in transfers:
                from_w = creator if tx["from"] == creator.id else receiver
                to_w = receiver if tx["to"] == receiver.id else creator
                amount_to_send = tx["amount"]
                
                signature = f"simulated_sig_{contract.id}_{datetime.utcnow().timestamp()}"
                
                # Применяем балансы
                from_w.balance -= amount_to_send
                to_w.balance += amount_to_send
                
                # Записываем операцию в историю транзакций
                new_tx = RubleTransaction(
                    sender_wallet_id=from_w.id,
                    receiver_wallet_id=to_w.id,
                    amount=amount_to_send,
                    status="completed",
                    smart_contract_id=contract.id,
                    signature=signature,
                    timestamp=datetime.utcnow() + timedelta(hours=3) # Используем МСК
                )
                session.add(new_tx)
                
            contract.status = "executed"
            contract.executed_at = datetime.utcnow()
            executed += 1
        else:
            # Контракт вернул False (условие еще не наступило).
            # Просто ждем следующего дня, средства остаются замороженными.
            pass
            
    await session.commit()
    log_info("Ruble EOD", f"Обработано контрактов: {processed}. Исполнено: {executed}. Упало с ошибкой: {failed}.")

async def run_ruble_eod():
    """
    Запускает воркер цифрового рубля
    """
    async with AsyncSessionLocal() as session:
        log_info("Ruble EOD", f"=== НАЧАЛО EOD ЦИФРОВОГО РУБЛЯ ===")
        await process_smart_contracts(session)
        log_info("Ruble EOD", f"=== EOD ЦИФРОВОГО РУБЛЯ ЗАВЕРШЕН ===")
