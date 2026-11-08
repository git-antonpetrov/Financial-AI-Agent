import ast
import sys
import time
from decimal import Decimal
from typing import Any

from src.simulations.smart_contracts.dsl.context import ContractContext


_MAX_SEQUENCE_REPEAT = 1000
_MAX_INT_LITERAL_DIGITS = 9


def _constant_int(node: ast.AST) -> int | None:
    """Возвращает целое значение константы либо None, если узел не является целым литералом."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value
    return None


def _sequence_like(node: ast.AST) -> bool:
    """Проверяет, порождает ли узел строку или коллекцию на уровне синтаксиса."""
    if isinstance(node, (ast.List, ast.Tuple, ast.BinOp, ast.JoinedStr)):
        return True
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _reject_sequence_multiplication(left: ast.AST, right: ast.AST) -> None:
    """Запрещает размножение строк и коллекций, включая обход через int(\"...\")."""
    for seq_node, factor_node in ((left, right), (right, left)):
        if not _sequence_like(seq_node):
            continue
        factor = _constant_int(factor_node)
        if factor is None or factor < 0 or factor > _MAX_SEQUENCE_REPEAT:
            raise SandboxExecutionError(
                "Размножение последовательности разрешено только на целый литерал от 0 до 1000"
            )
        if isinstance(seq_node, ast.Constant) and isinstance(seq_node.value, str):
            if len(seq_node.value) * factor > _MAX_SEQUENCE_REPEAT:
                raise SandboxExecutionError("Размножение строки превышает допустимый размер")


def _sandbox_int(value: Any = 0, base: int = 10) -> int:
    """Преобразует значение в int и отклоняет длинные строковые аргументы."""
    if isinstance(value, str):
        token = value.strip()
        digits = token[1:] if token.startswith(("+", "-")) else token
        if len(digits) > _MAX_INT_LITERAL_DIGITS:
            raise SandboxExecutionError("Слишком длинный аргумент int()")
        return int(token, base)
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise SandboxExecutionError("int() принимает только число или короткую строку")
    return int(value)


def _sandbox_mul(left: Any, right: Any) -> Any:
    """Умножает числа и ограничивает размножение строк и коллекций."""
    sequence = None
    factor = None
    if isinstance(left, (str, list, tuple, bytes)) and isinstance(right, int) and not isinstance(right, bool):
        sequence, factor = left, right
    elif isinstance(right, (str, list, tuple, bytes)) and isinstance(left, int) and not isinstance(left, bool):
        sequence, factor = right, left
    if sequence is not None and factor is not None:
        if factor < 0 or factor > _MAX_SEQUENCE_REPEAT or len(sequence) * factor > _MAX_SEQUENCE_REPEAT:
            raise SandboxExecutionError("Размножение последовательности превышает лимит песочницы")
        return sequence * factor
    if isinstance(left, (int, float, Decimal)) and isinstance(right, (int, float, Decimal)):
        if isinstance(left, bool) or isinstance(right, bool):
            raise SandboxExecutionError("Недопустимое умножение")
        return left * right
    raise SandboxExecutionError("Недопустимое умножение")


class _MultiplicationRewriter(ast.NodeTransformer):
    """Заменяет оператор умножения вызовом ограниченной функции песочницы."""

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        """Переписывает умножение в вызов _sandbox_mul."""
        node = self.generic_visit(node)
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Mult):
            return node
        return ast.Call(
            func=ast.Name(id="_sandbox_mul", ctx=ast.Load()),
            args=[node.left, node.right],
            keywords=[],
        )

    def visit_AugAssign(self, node: ast.AugAssign) -> ast.AST:
        """Переписывает составное умножение в вызов _sandbox_mul."""
        node = self.generic_visit(node)
        if not isinstance(node.op, ast.Mult):
            return node
        loaded = ast.Name(id=node.target.id, ctx=ast.Load()) if isinstance(node.target, ast.Name) else node.target
        return ast.Assign(
            targets=[node.target],
            value=ast.Call(
                func=ast.Name(id="_sandbox_mul", ctx=ast.Load()),
                args=[loaded, node.value],
                keywords=[],
            ),
        )


class SandboxExecutionError(Exception):
    """Исключение, возникающее при нарушении правил безопасности или ошибке выполнения песочницы."""


class SecurityASTVisitor(ast.NodeVisitor):
    """
    AST-валидатор для синтаксического анализа и выявления потенциально опасных конструкций
    в коде смарт-контракта до момента его компиляции и выполнения.
    """

    FORBIDDEN_CALLS = {
        "eval",
        "exec",
        "open",
        "compile",
        "__import__",
        "input",
        "breakpoint",
        "exit",
        "quit",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "format",
        "format_map",
    }

    def __init__(self):
        super().__init__()
        self.has_execute_function = False

    def visit_Module(self, node: ast.Module) -> None:
        for stmt in node.body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                raise SandboxExecutionError("Импорт модулей категорически запрещен в смарт-контрактах")
            if not isinstance(stmt, ast.FunctionDef):
                raise SandboxExecutionError(
                    "На верхнем уровне смарт-контракта разрешены только определения функций "
                    f"(FunctionDef), обнаружен {type(stmt).__name__}"
                )
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        raise SandboxExecutionError("Использование циклов (while) категорически запрещено в смарт-контрактах")

    def visit_For(self, node: ast.For) -> None:
        raise SandboxExecutionError("Использование циклов (for) категорически запрещено в смарт-контрактах")

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        raise SandboxExecutionError("Использование циклов (async for) категорически запрещено в смарт-контрактах")

    def visit_ListComp(self, node: ast.ListComp) -> None:
        raise SandboxExecutionError(
            "Использование списковых включений (list comprehensions) запрещено в смарт-контрактах"
        )

    def visit_SetComp(self, node: ast.SetComp) -> None:
        raise SandboxExecutionError("Использование set comprehensions запрещено в смарт-контрактах")

    def visit_DictComp(self, node: ast.DictComp) -> None:
        raise SandboxExecutionError("Использование dict comprehensions запрещено в смарт-контрактах")

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        raise SandboxExecutionError(
            "Использование генераторных выражений (generator expressions) запрещено в смарт-контрактах"
        )

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr in (
            "format",
            "format_map",
        ):
            raise SandboxExecutionError(f"Вызов метода '{node.func.attr}' категорически запрещен в смарт-контрактах")
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Pow):
            raise SandboxExecutionError(
                "Оператор возведения в степень (**) запрещен в смарт-контрактах (DoS protection)"
            )
        if isinstance(node.op, (ast.LShift, ast.RShift)):
            raise SandboxExecutionError("Битовые сдвиги запрещены в смарт-контрактах (DoS protection)")
        if isinstance(node.op, ast.Mult):
            _reject_sequence_multiplication(node.left, node.right)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.op, ast.Pow):
            raise SandboxExecutionError(
                "Оператор составного возведения в степень (**=) запрещен в смарт-контрактах (DoS protection)"
            )
        if isinstance(node.op, (ast.LShift, ast.RShift)):
            raise SandboxExecutionError("Составные битовые сдвиги запрещены в смарт-контрактах (DoS protection)")
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        raise SandboxExecutionError("Импорт модулей (import) категорически запрещен в смарт-контрактах")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        raise SandboxExecutionError("Импорт модулей (from ... import) категорически запрещен в смарт-контрактах")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_"):
            raise SandboxExecutionError(f"Запрещен доступ к приватным и специальным атрибутам ('{node.attr}')")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id.startswith("_"):
            raise SandboxExecutionError(
                f"Запрещено использование идентификаторов, начинающихся с подчеркивания ('{node.id}')"
            )
        if node.id in self.FORBIDDEN_CALLS:
            raise SandboxExecutionError(f"Запрещено обращение к небезопасной функции или объекту '{node.id}'")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Статический запрет рекурсии: запрет вызова функции внутри собственного тела
        for sub_node in ast.walk(node):
            if isinstance(sub_node, ast.Call):
                func_name = None
                if isinstance(sub_node.func, ast.Name):
                    func_name = sub_node.func.id
                if func_name == node.name:
                    raise SandboxExecutionError(
                        f"Рекурсивный вызов функции '{node.name}' категорически запрещен в смарт-контрактах"
                    )

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

    SAFE_BUILTINS: dict[str, Any] = {
        "True": True,
        "False": False,
        "None": None,
        "len": len,
        "round": round,
        "int": _sandbox_int,
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

    MAX_CODE_SIZE: int = 65536  # Максимальный размер исходного кода смарт-контракта (64 КБ)

    @classmethod
    def validate_code(cls, code: str) -> None:
        """
        Проверяет AST-дерево кода смарт-контракта на отсутствие опасных операций и наличие точки входа.
        """
        if not isinstance(code, str) or len(code.encode("utf-8")) > cls.MAX_CODE_SIZE:
            raise SandboxExecutionError(
                f"Размер исходного кода смарт-контракта превышает лимит {cls.MAX_CODE_SIZE} байт"
            )

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
        max_operations: int = 50000,
    ) -> tuple[bool, bool, list[dict[str, Any]], str | None]:
        """
        Исполняет проверенный код смарт-контракта в изолированном контексте с лимитом операций и времени.

        Аргументы:
            code: Исходный код смарт-контракта на Python.
            ctx: Контекст исполнения смарт-контракта.
            timeout_seconds: Лимит времени выполнения в секундах.
            max_operations: Максимальное число выполняемых шагов.

        Возвращает:
            Кортеж из признака успеха, признака завершения, списка переводов и текста ошибки.
        """
        try:
            # 1. Статический анализ безопасности (AST)
            cls.validate_code(code)

            # 2. Изолированная среда выполнения
            restricted_globals: dict[str, Any] = {
                "__builtins__": cls.SAFE_BUILTINS,
                "_sandbox_mul": _sandbox_mul,
            }
            local_scope: dict[str, Any] = {}

            tree = ast.parse(code)
            tree = _MultiplicationRewriter().visit(tree)
            ast.fix_missing_locations(tree)

            # 3. Мониторинг выполнения с лимитами ресурсов (инструкций и времени)
            # В соответствии с SEC-04 сторожевой таймер устанавливается ДО вызова exec()!
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
                # 4. Компиляция и объявление
                compiled_code = compile(tree, filename="<smart_contract>", mode="exec")
                exec(compiled_code, restricted_globals, local_scope)

                execute_func = local_scope.get("execute")
                if not callable(execute_func):
                    raise SandboxExecutionError("Объект 'execute' в смарт-контракте не является вызываемой функцией")

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
