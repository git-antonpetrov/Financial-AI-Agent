def get_client_context() -> str:
    """Возвращает информацию о текущем финансовом состоянии клиента."""
    return "У клиента на счету 1 000 000 рублей. Риск-профиль: Консервативный. Прошлых стратегий нет."

CLIENT_CONTEXT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_client_context",
        "description": "Получить финансовый контекст клиента (баланс, риск-профиль, история стратегий).",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
}
