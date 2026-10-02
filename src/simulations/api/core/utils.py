def to_dict(obj):
    """
    [DEPRECATED] Безопасная конвертация SQLAlchemy ORM-модели в словарь.
    Устарело: используйте типизированные Pydantic Response модели из
    src.simulations.api.schemas.responses.
    """
    if obj is None:
        return None
    d = obj.__dict__.copy()
    d.pop('_sa_instance_state', None)
    return d

def to_dict_list(obj_list):
    """
    [DEPRECATED] Конвертация списка ORM-моделей в список словарей.
    Устарело: используйте типизированные Pydantic Response модели из
    src.simulations.api.schemas.responses.
    """
    return [to_dict(obj) for obj in obj_list]
