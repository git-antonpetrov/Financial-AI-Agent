"""Памятный клиент Redis для тестов: SET NX и очередь без сети."""


class MemoryRedis:
    """Хранит ключи и списки в процессе и не открывает сокет."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}

    def ping(self) -> bool:
        """Подтверждает, что клиент готов."""
        return True

    def set(self, key: str, value: str, ex: int | None = None, nx: bool = False, **kwargs: object) -> bool | None:
        """Записывает ключ. При nx и существующем ключе возвращает None.

        Аргументы:
            key: Ключ.
            value: Значение.
            ex: Срок жизни. Тест его не применяет.
            nx: Запрещает перезапись существующего ключа.
            kwargs: Прочие аргументы redis-py, которые тест игнорирует.

        Возвращает:
            True, если ключ записан, иначе None.
        """
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def setex(self, key: str, ttl: int, value: str) -> bool:
        """Записывает ключ с игнорируемым в тесте сроком жизни."""
        self.store[key] = value
        return True

    def get(self, key: str) -> str | None:
        """Возвращает сохранённое значение."""
        return self.store.get(key)

    def exists(self, key: str) -> int:
        """Возвращает 1, если ключ есть."""
        return 1 if key in self.store else 0

    def delete(self, *keys: str) -> int:
        """Удаляет перечисленные ключи."""
        removed = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                removed += 1
        return removed

    def incr(self, key: str) -> int:
        """Увеличивает целочисленный счётчик."""
        current = int(self.store.get(key) or 0) + 1
        self.store[key] = str(current)
        return current

    def expire(self, key: str, ttl: int) -> bool:
        """Принимает срок жизни и оставляет значение."""
        return key in self.store

    def ttl(self, key: str) -> int:
        """Возвращает положительный остаток, чтобы код не считал ключ бессрочным."""
        return 30 if key in self.store else -2

    def lpush(self, key: str, value: str) -> int:
        """Кладёт значение в начало списка."""
        bucket = self.lists.setdefault(key, [])
        bucket.insert(0, value)
        return len(bucket)

    def publish(self, channel: str, message: str) -> int:
        """Имитирует публикацию без подписчиков."""
        return 0

    def close(self) -> None:
        """Закрывает клиент без сетевого вызова."""
        return None
