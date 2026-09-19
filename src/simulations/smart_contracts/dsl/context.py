from datetime import date

class ContractContext:
    """
    Контекст исполнения смарт-контракта. 
    Это единственный способ, которым код контракта может взаимодействовать с внешним миром.
    Он не имеет прямого доступа к базе данных.
    """
    def __init__(self, contract_id: str, creator_id: str, receiver_id: str, amount: float, condition_status: str):
        self.contract_id = contract_id
        self.creator_id = creator_id
        self.receiver_id = receiver_id
        self.amount = amount
        self.condition_status = condition_status
        self.current_date = date.today()
        
        # Внутреннее состояние песочницы, куда контракт может делать запросы
        self._requested_transfers = []
        self._is_completed = False

    def get_condition_status(self) -> str:
        """Возвращает статус условия контракта ('pending', 'fulfilled', 'failed')"""
        return self.condition_status

    def transfer(self, from_wallet: str, to_wallet: str, amount: float):
        """Запрашивает перевод средств. Физически перевод произойдет только после успешного завершения контракта."""
        if from_wallet != self.creator_id and from_wallet != self.receiver_id:
            raise ValueError("Можно переводить только между участниками контракта (отправитель)")
        if to_wallet != self.creator_id and to_wallet != self.receiver_id:
            raise ValueError("Можно переводить только между участниками контракта (получатель)")
        
        self._requested_transfers.append({
            "from": from_wallet,
            "to": to_wallet,
            "amount": amount
        })

    def complete(self):
        """Контракт вызывает этот метод, когда считает, что полностью отработал и его можно закрывать"""
        self._is_completed = True
