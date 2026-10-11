import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from src.simulations.smart_contracts.dsl.context import ContractContext
from src.simulations.smart_contracts.sandbox.executor import SmartContractSandbox, SandboxExecutionError


def test_valid_smart_contract_execution():
    """Тест штатного исполнения валидного смарт-контракта."""
    code = """
def execute(ctx):
    if ctx.get_condition_status() == 'fulfilled':
        ctx.transfer(ctx.creator_id, ctx.receiver_id, ctx.amount)
        return True
    return False
"""
    ctx = ContractContext(
        contract_id="c-1",
        creator_id="w-1",
        receiver_id="w-2",
        amount=100.0,
        condition_status="fulfilled"
    )
    success, is_completed, transfers, error = SmartContractSandbox.execute_contract(code, ctx)
    assert success is True
    assert is_completed is True
    assert error is None
    assert len(transfers) == 1
    assert transfers[0] == {"from": "w-1", "to": "w-2", "amount": 100.0}


def test_rce_via_subclasses_blocked():
    """Тест блокировки попытки RCE через обход классов dunder-атрибутов."""
    code = """
def execute(ctx):
    subclasses = ().__class__.__base__.__subclasses__()
    return True
"""
    ctx = ContractContext("c-1", "w-1", "w-2", 100.0, "fulfilled")
    success, is_completed, transfers, error = SmartContractSandbox.execute_contract(code, ctx)
    assert success is False
    assert is_completed is False
    assert "Запрещен доступ к приватным и специальным атрибутам" in error


def test_import_statement_blocked():
    """Тест блокировки оператора import."""
    code = """
import os
def execute(ctx):
    return True
"""
    ctx = ContractContext("c-1", "w-1", "w-2", 100.0, "fulfilled")
    success, is_completed, transfers, error = SmartContractSandbox.execute_contract(code, ctx)
    assert success is False
    assert "Импорт модулей" in error


def test_from_import_statement_blocked():
    """Тест блокировки оператора from ... import."""
    code = """
from sys import exit
def execute(ctx):
    return True
"""
    ctx = ContractContext("c-1", "w-1", "w-2", 100.0, "fulfilled")
    success, is_completed, transfers, error = SmartContractSandbox.execute_contract(code, ctx)
    assert success is False
    assert "Импорт модулей" in error


def test_forbidden_builtins_blocked():
    """Тест блокировки вызова запрещенных встроенных функций (open, eval, exec)."""
    code = """
def execute(ctx):
    f = open('/etc/passwd', 'r')
    return True
"""
    ctx = ContractContext("c-1", "w-1", "w-2", 100.0, "fulfilled")
    success, is_completed, transfers, error = SmartContractSandbox.execute_contract(code, ctx)
    assert success is False
    assert "Запрещено обращение к небезопасной функции или объекту 'open'" in error


def test_private_attribute_access_blocked():
    """Тест блокировки доступа к внутренним приватным полям контекста."""
    code = """
def execute(ctx):
    ctx._is_completed = True
    return True
"""
    ctx = ContractContext("c-1", "w-1", "w-2", 100.0, "fulfilled")
    success, is_completed, transfers, error = SmartContractSandbox.execute_contract(code, ctx)
    assert success is False
    assert "Запрещен доступ к приватным и специальным атрибутам ('_is_completed')" in error


def test_missing_execute_function():
    """Тест обнаружения отсутствия функции execute(ctx)."""
    code = """
def run(ctx):
    return True
"""
    ctx = ContractContext("c-1", "w-1", "w-2", 100.0, "fulfilled")
    success, is_completed, transfers, error = SmartContractSandbox.execute_contract(code, ctx)
    assert success is False
    assert "В смарт-контракте не найдена обязательная функция 'execute(ctx)'" in error


def test_execution_timeout_and_ops_limit():
    """Тест прерывания рекурсии по лимиту шагов и таймауту."""
    code = """
def execute(ctx):
    def loop(n):
        return loop(n + 1)
    return loop(0)
"""
    ctx = ContractContext("c-1", "w-1", "w-2", 100.0, "fulfilled")
    success, is_completed, transfers, error = SmartContractSandbox.execute_contract(
        code, ctx, timeout_seconds=1.0, max_operations=1000
    )
    assert success is False
    assert ("Превышен лимит вычислительной сложности" in error or "Превышено максимальное время выполнения" in error)


def test_while_and_for_loops_rejected():
    """Тест блокировки циклов while и for в коде смарт-контракта (SEC-04)."""
    code_while = """
def execute(ctx):
    while True:
        pass
    return True
"""
    ctx = ContractContext("c-1", "w-1", "w-2", 100.0, "fulfilled")
    success, _, _, error = SmartContractSandbox.execute_contract(code_while, ctx)
    assert success is False
    assert "Использование циклов (while)" in error

    code_for = """
def execute(ctx):
    for i in [1, 2, 3]:
        pass
    return True
"""
    success, _, _, error = SmartContractSandbox.execute_contract(code_for, ctx)
    assert success is False
    assert "Использование циклов (for)" in error


def test_top_level_expressions_rejected():
    """Тест блокировки выражений и логики на верхнем уровне модуля вне функций (SEC-04)."""
    code = """
x = 10
def execute(ctx):
    return True
"""
    ctx = ContractContext("c-1", "w-1", "w-2", 100.0, "fulfilled")
    success, _, _, error = SmartContractSandbox.execute_contract(code, ctx)
    assert success is False
    assert "На верхнем уровне смарт-контракта разрешены только определения функций" in error


def test_str_format_methods_blocked():
    """Тест блокировки вызовов методов format и format_map (SEC-05)."""
    code = """
def execute(ctx):
    s = "{0}".format(ctx)
    return True
"""
    ctx = ContractContext("c-1", "w-1", "w-2", 100.0, "fulfilled")
    success, _, _, error = SmartContractSandbox.execute_contract(code, ctx)
    assert success is False
    assert "Вызов метода 'format' категорически запрещен" in error


def test_context_dunder_isolation():
    """Тест изоляции dunder-атрибутов (__class__, __init__, __globals__) объекта контекста (SEC-05)."""
    ctx = ContractContext("c-1", "w-1", "w-2", 100.0, "fulfilled")
    with pytest.raises(AttributeError, match="категорически запрещен"):
        _ = ctx.__class__

    with pytest.raises(AttributeError, match="категорически запрещен"):
        _ = ctx.__init__

    with pytest.raises(AttributeError, match="категорически запрещен"):
        _ = ctx.__globals__

