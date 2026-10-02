import re

DB_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]{1,63}$")


def validate_db_name(db_name: str) -> str:
    """
    Валидирует имя базы данных PostgreSQL для безопасного использования в DDL-командах (CREATE DATABASE).
    
    Защищает от SQL-инъекций:
    - Разрешены только латинские буквы, цифры и символ подчеркивания `_`.
    - Длина строго от 1 до 63 символов (максимальная длина идентификатора PostgreSQL NAMEDATALEN - 1).
    - Запрещены кавычки, точки с запятой, пробелы, спецсимволы и управляющие последовательности.
    """
    if not isinstance(db_name, str):
        raise ValueError(f"Имя базы данных должно быть строкой, получено: {type(db_name).__name__}")
        
    cleaned_name = db_name.strip()
    if not cleaned_name:
        raise ValueError("Имя базы данных не может быть пустым.")
        
    if not DB_NAME_PATTERN.match(cleaned_name):
        raise ValueError(
            f"Недопустимое имя базы данных '{db_name}'. "
            "Имя должно содержать только символы [a-zA-Z0-9_] и иметь длину от 1 до 63 символов."
        )
        
    return cleaned_name
