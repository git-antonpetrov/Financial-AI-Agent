def to_dict(obj):
    """
    Безопасная конвертация SQLAlchemy ORM-модели в словарь.
    Удаляет служебные поля типа _sa_instance_state.
    """
    if obj is None:
        return None
    d = obj.__dict__.copy()
    d.pop('_sa_instance_state', None)
    return d

def to_dict_list(obj_list):
    """
    Конвертация списка ORM-моделей в список словарей.
    """
    return [to_dict(obj) for obj in obj_list]
