from datetime import date
from decimal import Decimal
from typing import Any


class ContractContext:
    """
    Контекст безопасного исполнения смарт-контракта цифрового рубля.
    Предоставляет DSL-интерфейс для взаимодействия логики контракта с заблокированными средствами.
    Не имеет прямого доступа к базе данных.
    """

    def __init__(
        self,
        contract_id: str,
        creator_id: str,
        receiver_id: str,
        amount: float | Decimal,
        condition_status: str,
    ):
        self._contract_id = str(contract_id)
        self._creator_id = str(creator_id)
        self._receiver_id = str(receiver_id)
        self._amount = Decimal(str(amount))
        self._condition_status = str(condition_status)
        self._current_date = date.today()

        # Внутреннее состояние песочницы для регистрации запросов на перевод
        self._requested_transfers: list[dict[str, Any]] = []
        self._is_completed: bool = False
        self._initialized: bool = True

    @property
    def contract_id(self) -> str:
        return self._contract_id

    @property
    def creator_id(self) -> str:
        return self._creator_id

    @property
    def receiver_id(self) -> str:
        return self._receiver_id

    @property
    def amount(self) -> Decimal:
        return self._amount

    @property
    def condition_status(self) -> str:
        return self._condition_status

    @property
    def current_date(self) -> date:
        return self._current_date

    FORBIDDEN_ATTRS = {
        "__class__",
        "__init__",
        "__globals__",
        "__subclasses__",
        "__bases__",
        "__base__",
        "__mro__",
        "__dict__",
        "__code__",
        "__func__",
        "__closure__",
    }

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_initialized", False):
            if name.startswith("_") or name in (
                "contract_id",
                "creator_id",
                "receiver_id",
                "amount",
                "condition_status",
                "current_date",
            ):
                raise AttributeError(f"Модификация защищенного атрибута '{name}' контекста запрещена")
        super().__setattr__(name, value)

    def __getattribute__(self, name: str) -> Any:
        if name in ContractContext.FORBIDDEN_ATTRS:
            raise AttributeError(
                f"Доступ к приватному dunder-атрибуту '{name}' объекта ContractContext категорически запрещен (SEC-05)"
            )
        return super().__getattribute__(name)

    def __format__(self, format_spec: str) -> str:
        return "ContractContext"

    def get_condition_status(self) -> str:
        """Возвращает статус внешнего условия контракта ('pending', 'fulfilled', 'failed')."""
        return self.condition_status

    def transfer(self, from_wallet: str, to_wallet: str, amount: float | Decimal | str) -> None:
        """
        Запрашивает перевод средств в рамках смарт-контракта.
        Перевод физически исполняется воркером только после успешного завершения логики контракта.

        Правила безопасности:
        1. Списание средств возможно только из заблокированного депозита создателя контракта (creator_id).
        2. Получателем может выступать только участник контракта (receiver_id или возврат creator_id).
        3. Отправитель и получатель не могут совпадать.
        4. Сумма перевода должна быть строго положительной.
        5. Суммарный объем переводов контракта не может превышать сумму залога (self.amount).
        """
        try:
            num_amount = Decimal(str(amount))
        except Exception:
            raise ValueError(f"Некорректный формат суммы перевода: {amount}")

        if num_amount <= Decimal("0.00"):
            raise ValueError("Сумма перевода должна быть строго больше нуля")

        if from_wallet != self.creator_id:
            raise ValueError("Списание средств возможно только из заблокированного депозита создателя контракта")

        if to_wallet not in (self.creator_id, self.receiver_id):
            raise ValueError("Получателем средств может быть только участник смарт-контракта")

        if from_wallet == to_wallet:
            raise ValueError("Счет списания и счет зачисления не могут совпадать")

        current_total = sum(
            (Decimal(str(t["amount"])) for t in self._requested_transfers),
            Decimal("0.00"),
        )
        if current_total + num_amount > self.amount:
            raise ValueError(
                f"Суммарный объем переводов ({current_total + num_amount:.2f}) "
                f"превышает лимит средств смарт-контракта ({self.amount:.2f})"
            )

        self._requested_transfers.append(
            {
                "from": from_wallet,
                "to": to_wallet,
                "amount": num_amount.quantize(Decimal("0.01")),
            }
        )

    def complete(self) -> None:
        """Помечает контракт как завершенный (готов к расчету)."""
        super().__setattr__("_is_completed", True)
