import asyncio
from unittest.mock import AsyncMock, MagicMock
from decimal import Decimal
from src.simulations.workers.digital_eod.tasks import process_single_contract


class DummyWallet:
    def __init__(self, id, balance, frozen_balance):
        self.id = id
        self.balance = balance
        self.frozen_balance = frozen_balance


class DummyContract:
    def __init__(self, id, creator_wallet_id, receiver_wallet_id, amount, contract_code, condition_status="active"):
        self.id = id
        self.creator_wallet_id = creator_wallet_id
        self.receiver_wallet_id = receiver_wallet_id
        self.amount = amount
        self.contract_code = contract_code
        self.condition_status = condition_status
        self.status = "active"
        self.error_message = None
        self.executed_at = None


def test_process_contract_success_full_transfer():
    async def _test():
        creator = DummyWallet(id=1, balance=Decimal("100.00"), frozen_balance=Decimal("50.00"))
        receiver = DummyWallet(id=2, balance=Decimal("20.00"), frozen_balance=Decimal("0.00"))
        
        code = """def execute(context):
    if context.condition_status == 'active':
        context.transfer(from_wallet=context.creator_id, to_wallet=context.receiver_id, amount=Decimal('50.00'))
        context.complete()
"""
        contract = DummyContract(
            id=10,
            creator_wallet_id=1,
            receiver_wallet_id=2,
            amount=Decimal("50.00"),
            contract_code=code,
            condition_status="active"
        )

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        
        creator_res = MagicMock()
        creator_res.scalars.return_value.first.return_value = creator
        
        receiver_res = MagicMock()
        receiver_res.scalars.return_value.first.return_value = receiver

        mock_session.execute.side_effect = [creator_res, receiver_res]

        status = await process_single_contract(mock_session, contract)

        assert status == "executed"
        assert contract.status == "executed"
        assert contract.error_message is None
        # Проверяет списание замороженного баланса
        assert creator.frozen_balance == Decimal("0.00")
        # Баланс создателя не изменился (был 100, остался 100, так как 50 было во frozen)
        assert creator.balance == Decimal("100.00")
        # Получатель получил 50
        assert receiver.balance == Decimal("70.00")
        # Транзакция добавлена в сессию
        assert mock_session.add.called

    asyncio.run(_test())


def test_process_contract_success_partial_transfer_with_refund():
    async def _test():
        # Создатель заморозил 50, но контракт перевел только 30
        creator = DummyWallet(id=1, balance=Decimal("100.00"), frozen_balance=Decimal("50.00"))
        receiver = DummyWallet(id=2, balance=Decimal("20.00"), frozen_balance=Decimal("0.00"))
        
        code = """def execute(context):
    if context.condition_status == 'active':
        context.transfer(from_wallet=context.creator_id, to_wallet=context.receiver_id, amount=Decimal('30.00'))
        context.complete()
"""
        contract = DummyContract(
            id=11,
            creator_wallet_id=1,
            receiver_wallet_id=2,
            amount=Decimal("50.00"),
            contract_code=code,
            condition_status="active"
        )

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        creator_res = MagicMock()
        creator_res.scalars.return_value.first.return_value = creator
        receiver_res = MagicMock()
        receiver_res.scalars.return_value.first.return_value = receiver
        mock_session.execute.side_effect = [creator_res, receiver_res]

        status = await process_single_contract(mock_session, contract)

        assert status == "executed"
        assert contract.status == "executed"
        # 50 списано из frozen
        assert creator.frozen_balance == Decimal("0.00")
        # 20 неизрасходованного остатка возвращено создателю
        assert creator.balance == Decimal("120.00")
        # Получатель получил 30
        assert receiver.balance == Decimal("50.00")

    asyncio.run(_test())


def test_process_contract_failed_refunds_frozen_balance():
    async def _test():
        # Если контракт упал с ошибкой, замороженные 50 должны вернуться на balance
        creator = DummyWallet(id=1, balance=Decimal("100.00"), frozen_balance=Decimal("50.00"))
        receiver = DummyWallet(id=2, balance=Decimal("20.00"), frozen_balance=Decimal("0.00"))
        
        # Запрещенный код с __import__
        code = """def execute(context):
    __import__('os').system('ls')
"""
        contract = DummyContract(
            id=12,
            creator_wallet_id=1,
            receiver_wallet_id=2,
            amount=Decimal("50.00"),
            contract_code=code
        )

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        creator_res = MagicMock()
        creator_res.scalars.return_value.first.return_value = creator
        receiver_res = MagicMock()
        receiver_res.scalars.return_value.first.return_value = receiver
        mock_session.execute.side_effect = [creator_res, receiver_res]

        status = await process_single_contract(mock_session, contract)

        assert status == "failed"
        assert contract.status == "failed"
        assert "security violation" in contract.error_message.lower() or "запрещен" in contract.error_message.lower()
        # Замороженные деньги вернулись
        assert creator.frozen_balance == Decimal("0.00")
        assert creator.balance == Decimal("150.00")
        assert receiver.balance == Decimal("20.00")

    asyncio.run(_test())


def test_process_contract_condition_not_met_pending():
    async def _test():
        # Условие не выполнено -> контракт остается pending, деньги не трогаются
        creator = DummyWallet(id=1, balance=Decimal("100.00"), frozen_balance=Decimal("50.00"))
        receiver = DummyWallet(id=2, balance=Decimal("20.00"), frozen_balance=Decimal("0.00"))
        
        code = """def execute(context):
    if context.condition_status == 'delivered':
        context.transfer(from_wallet=context.creator_id, to_wallet=context.receiver_id, amount=Decimal('50.00'))
        context.complete()
"""
        contract = DummyContract(
            id=13,
            creator_wallet_id=1,
            receiver_wallet_id=2,
            amount=Decimal("50.00"),
            contract_code=code,
            condition_status="waiting"
        )

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        creator_res = MagicMock()
        creator_res.scalars.return_value.first.return_value = creator
        receiver_res = MagicMock()
        receiver_res.scalars.return_value.first.return_value = receiver
        mock_session.execute.side_effect = [creator_res, receiver_res]

        status = await process_single_contract(mock_session, contract)

        assert status == "pending"
        assert contract.status == "active"
        assert creator.frozen_balance == Decimal("50.00")
        assert creator.balance == Decimal("100.00")
        assert receiver.balance == Decimal("20.00")

    asyncio.run(_test())
