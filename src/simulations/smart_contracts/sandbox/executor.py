import traceback
from src.simulations.smart_contracts.dsl.context import ContractContext

class SandboxExecutionError(Exception):
    pass

class SmartContractSandbox:
    @staticmethod
    def execute_contract(code: str, ctx: ContractContext):
        """
        Изолированная песочница для выполнения кода смарт-контракта.
        Код смарт-контракта должен содержать функцию `def execute(ctx):`
        Возвращает (success: bool, is_completed: bool, transfers: list, error_message: str|None)
        """
        # Создаем изолированное пространство имен
        # Запрещаем все встроенные функции (например __import__, open, eval), чтобы обезопасить песочницу
        restricted_globals = {
            "__builtins__": {
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
                "Exception": Exception,
                "ValueError": ValueError
            }
        }
        
        local_scope = {}
        
        try:
            # Компилируем и исполняем объявление функции
            exec(code, restricted_globals, local_scope)
            
            if 'execute' not in local_scope:
                raise SandboxExecutionError("В смарт-контракте не найдена функция 'execute(ctx)'")
            
            execute_func = local_scope['execute']
            
            # Запускаем функцию из смарт-контракта
            result = execute_func(ctx)
            
            # Контракт может сам решить, что он выполнился до конца, вернув True,
            # либо вызвав ctx.complete()
            is_completed = bool(result) or ctx._is_completed
            
            return True, is_completed, ctx._requested_transfers, None
            
        except Exception as e:
            # Перехватываем любую ошибку (как синтаксическую, так и рантайм)
            error_trace = traceback.format_exc()
            return False, False, [], error_trace
