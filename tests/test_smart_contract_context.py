import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from src.simulations.smart_contracts.dsl.context import ContractContext


def test_valid_transfer_within_limit():
    ctx = ContractContext("c-1", "w-creator", "w-receiver", 1000.0, "fulfilled")
    ctx.transfer("w-creator", "w-receiver", 500.0)
    ctx.transfer("w-creator", "w-receiver", 500.0)
    assert len(ctx._requested_transfers) == 2
    assert sum(t["amount"] for t in ctx._requested_transfers) == 1000.0


def test_transfer_exceeding_contract_amount():
    ctx = ContractContext("c-1", "w-creator", "w-receiver", 100.0, "fulfilled")
    with pytest.raises(ValueError, match="превышает лимит"):
        ctx.transfer("w-creator", "w-receiver", 100.01)


def test_cumulative_transfers_exceeding_limit():
    ctx = ContractContext("c-1", "w-creator", "w-receiver", 100.0, "fulfilled")
    ctx.transfer("w-creator", "w-receiver", 60.0)
    with pytest.raises(ValueError, match="превышает лимит"):
        ctx.transfer("w-creator", "w-receiver", 40.01)


def test_transfer_from_non_creator_rejected():
    ctx = ContractContext("c-1", "w-creator", "w-receiver", 100.0, "fulfilled")
    with pytest.raises(ValueError, match="только из заблокированного депозита создателя"):
        ctx.transfer("w-receiver", "w-creator", 50.0)


def test_transfer_to_unknown_wallet_rejected():
    ctx = ContractContext("c-1", "w-creator", "w-receiver", 100.0, "fulfilled")
    with pytest.raises(ValueError, match="только участник смарт-контракта"):
        ctx.transfer("w-creator", "w-hacker", 50.0)


def test_transfer_to_self_rejected():
    ctx = ContractContext("c-1", "w-creator", "w-receiver", 100.0, "fulfilled")
    with pytest.raises(ValueError, match="не могут совпадать"):
        ctx.transfer("w-creator", "w-creator", 50.0)


def test_transfer_negative_or_zero_amount():
    ctx = ContractContext("c-1", "w-creator", "w-receiver", 100.0, "fulfilled")
    with pytest.raises(ValueError, match="строго больше нуля"):
        ctx.transfer("w-creator", "w-receiver", 0.0)
    with pytest.raises(ValueError, match="строго больше нуля"):
        ctx.transfer("w-creator", "w-receiver", -10.0)
