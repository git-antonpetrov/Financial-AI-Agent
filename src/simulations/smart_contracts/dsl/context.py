from datetime import date
from decimal import Decimal
from typing import List, Dict, Any, Union


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
        amount: Union[float, Decimal],
        condition_status: str
    ):
        self.contract_id = str(contract_id)
        self.creator_id = str(creator_id)
        self.receiver_id = str(receiver_id)
        self.amount = Decimal(str(amount))
        self.condition_status = str(condition_status)
        self.current_date = date.today()
        
        # Внутреннее состояние песочницы для регистрации запросов на перевод
        self._requested_transfers: List[Dict[str, Any]] = []
        self._is_completed: bool = False

    def get_condition_status(self) -> str:
        """Возвращает статус внешнего условия контракта ('pending', 'fulfilled', 'failed')."""
        return self.condition_status

    def transfer(self, from_wallet: str, to_wallet: str, amount: Union[float, Decimal, int, str]) -> None:
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

        current_total = sum((Decimal(str(t["amount"])) for t in self._requested_transfers), Decimal("0.00"))
        if current_total + num_amount > self.amount:
            raise ValueError(
                f"Суммарный объем переводов ({current_total + num_amount:.2f}) "
                f"превышает лимит средств смарт-контракта ({self.amount:.2f})"
            )

        self._requested_transfers.append({
            "from": from_wallet,
            "to": to_wallet,
            "amount": round(num_amount, 2)
        })

    def complete(self) -> None:
        """Помечает контракт как завершенный (готов к расчету)."""
        self._is_completed = True
