import ast
import sys
import time
from decimal import Decimal
from typing import Tuple, List, Dict, Any, Optional

from src.simulations.smart_contracts.dsl.context import ContractContext


class SandboxExecutionError(Exception):
    """Исключение, возникающее при нарушении правил безопасности или ошибке выполнения песочницы."""
    pass


class SecurityASTVisitor(ast.NodeVisitor):
    """
    AST-валидатор для синтаксического анализа и выявления потенциально опасных конструкций
    в коде смарт-контракта до момента его компиляции и выполнения.
    """
    FORBIDDEN_CALLS = {
        "eval", "exec", "open", "compile", "__import__",
        "input", "breakpoint", "exit", "quit", "getattr", "setattr", "delattr", "globals", "locals"
    }

    def __init__(self):
        super().__init__()
        self.has_execute_function = False

    def visit_Import(self, node: ast.Import) -> None:
        raise SandboxExecutionError("Импорт модулей (import) категорически запрещен в смарт-контрактах")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        raise SandboxExecutionError("Импорт модулей (from ... import) категорически запрещен в смарт-контрактах")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_"):
            raise SandboxExecutionError(
                f"Запрещен доступ к приватным и специальным атрибутам ('{node.attr}')"
            )
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id.startswith("_"):
            raise SandboxExecutionError(
                f"Запрещено использование идентификаторов, начинающихся с подчеркивания ('{node.id}')"
            )
        if node.id in self.FORBIDDEN_CALLS:
            raise SandboxExecutionError(
                f"Запрещено обращение к небезопасной функции или объекту '{node.id}'"
            )
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.name == "execute":
            self.has_execute_function = True
            total_args = len(node.args.args)
            if total_args != 1:
                raise SandboxExecutionError(
                    f"Функция 'execute' должна принимать ровно один аргумент (ctx), получено аргументов: {total_args}"
                )
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        raise SandboxExecutionError("Асинхронные функции (async def) запрещены в смарт-контрактах")


class SmartContractSandbox:
    """
    Изолированная и защищенная песочница для выполнения DSL-кода смарт-контрактов цифрового рубля.
    """
    SAFE_BUILTINS: Dict[str, Any] = {
        "True": True,
        "False": False,
        "None": None,
        "len": len,
        "round": round,
        "int": int,
        "float": float,
        "str": str,
        "bool": bool,
        "list": list,
        "dict": dict,
        "set": set,
        "tuple": tuple,
        "min": min,
        "max": max,
        "abs": abs,
        "sum": sum,
        "Decimal": Decimal,
        "Exception": Exception,
        "ValueError": ValueError,
    }

    @classmethod
    def validate_code(cls, code: str) -> None:
        """
        Проверяет AST-дерево кода смарт-контракта на отсутствие опасных операций и наличие точки входа.
        """
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            raise SandboxExecutionError(f"Синтаксическая ошибка в смарт-контракте: {e}")

        visitor = SecurityASTVisitor()
        visitor.visit(tree)

        if not visitor.has_execute_function:
            raise SandboxExecutionError("В смарт-контракте не найдена обязательная функция 'execute(ctx)'")

    @classmethod
    def execute_contract(
        cls,
        code: str,
        ctx: ContractContext,
        timeout_seconds: float = 3.0,
        max_operations: int = 50000
    ) -> Tuple[bool, bool, List[Dict[str, Any]], Optional[str]]:
        """
        Исполняет проверенный код смарт-контракта в изолированном контексте с лимитом операций и времени.
        
        Args:
            code (str): Исходный код смарт-контракта на Python.
            ctx (ContractContext): Контекст исполнения смарт-контракта.
            timeout_seconds (float): Лимит времени выполнения в секундах.
            max_operations (int): Максимальное число выполняемых шагов/инструкций.
            
        Returns:
            Tuple[bool, bool, List[Dict[str, Any]], Optional[str]]:
                - success: True, если контракт отработал без ошибок.
                - is_completed: True, если контракт завершил свою логику (готов к расчету).
                - transfers: список запрошенных переводов.
                - error_message: текст ошибки при неудаче или None.
        """
        try:
            # 1. Статический анализ безопасности (AST)
            cls.validate_code(code)

            # 2. Изолированная среда выполнения
            restricted_globals: Dict[str, Any] = {
                "__builtins__": cls.SAFE_BUILTINS
            }
            local_scope: Dict[str, Any] = {}

            # 3. Компиляция и объявление
            compiled_code = compile(code, filename="<smart_contract>", mode="exec")
            exec(compiled_code, restricted_globals, local_scope)

            execute_func = local_scope.get("execute")
            if not callable(execute_func):
                raise SandboxExecutionError("Объект 'execute' в смарт-контракте не является вызываемой функцией")

            # 4. Мониторинг выполнения с лимитами ресурсов (инструкций и времени)
            start_time = time.time()
            ops_count = 0

            def trace_limits(frame, event, arg):
                nonlocal ops_count
                ops_count += 1
                if ops_count > max_operations:
                    raise SandboxExecutionError(
                        f"Превышен лимит вычислительной сложности смарт-контракта ({max_operations} операций)"
                    )
                if (time.time() - start_time) > timeout_seconds:
                    raise SandboxExecutionError(
                        f"Превышено максимальное время выполнения смарт-контракта ({timeout_seconds} сек)"
                    )
                return trace_limits

            sys.settrace(trace_limits)
            try:
                result = execute_func(ctx)
            finally:
                sys.settrace(None)

            # Контракт завершен, если функция вернула True или был вызван ctx.complete()
            is_completed = bool(result) or ctx._is_completed
            return True, is_completed, ctx._requested_transfers, None

        except SandboxExecutionError as e:
            return False, False, [], str(e)
        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            return False, False, [], error_msg
