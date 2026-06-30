def get_cbr_key_rate() -> str:
    """Возвращает текущую ключевую ставку ЦБ РФ."""
    return "Текущая ключевая ставка ЦБ РФ: 16% годовых."

CBR_RATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_cbr_key_rate",
        "description": "Получить текущую ключевую ставку Центробанка РФ.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
}
